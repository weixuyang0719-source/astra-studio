"""Re-run DuET with the project's beat map as a decoding constraint.

DuET writes each note as fields (bar, quarter, sub-beat, duration, hand, bar
length, meter, key, voice, grace). Here the bar, bar length and meter come
from the beat map, and the bar/position may only be chosen inside a window
around where the note was played on that map. Within the window DuET's own
scores (plus a mild timing term) choose the sub-beat; DuET alone chooses
duration, hand, voice, key and grace. Notes are therefore written on the
user's grid during decoding, never snapped onto it afterwards.

Speed (all on the CPU, measured faster than the laptop GPU, identical rows):
1. A resident service keeps the model loaded and each window's encoding
   cached, so an update does not pay process start-up and model loading.
2. Notes whose result is already known (the previous window's notes, and this
   window's notes before the first changed one) are fed to the decoder in one
   batched pass instead of one step per note.
3. After the change, decoding stops once a run of notes is written exactly as
   before and nothing later differs; the earlier result is spliced in.
Whole windows whose inputs are unchanged are reused without decoding.

Output: a prediction cache with the usual DuET files plus ``constrained.json``
(absolute beat position of every note) and ``windows.json`` (reuse records).
Vendored DuET code is not modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from time import monotonic

VERSION = 3
WINDOW = .75      # quarters either side of the played position
SIGMA = .15       # timing term width, quarters
MAX_DECODE_BAR = 32.  # longer (free) bars are split; DuET positions stop at 64 quarters
STOP_AFTER = 12   # identical notes in a row after the last changed one before splicing
KEEP_CACHES = 6
SERVICE_IDLE_SECONDS = 20 * 60
ARTIFACTS = ('score.npz', 'frames.csv', 'report.json', 'notes.json', 'constrained.json', 'windows.json')


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def decode_bars(tempo, last_beat):
    """Bars to decode into: notation bars, a lead-in before bar 1, and bars past
    a confirmed end (so late releases still decode; notation decides later)."""
    from .timing import TempoMap
    from .duet_notation import reference_bars
    from .score_boundary import confirmed_end_beat
    clock = TempoMap(json.loads(json.dumps(tempo)))
    end = confirmed_end_beat(clock)
    starts, lengths, meters = reference_bars(clock, min(last_beat, end) if end is not None else last_beat)
    bars = [(float(s), float(l), *(int(v) for v in m.split('/'))) for s, l, m in zip(starts, lengths, meters)]
    if not bars:
        raise ValueError('拍点图没有可写谱的小节')
    if bars[0][0] > 1e-9:
        bars.insert(0, (0.0, bars[0][0], bars[0][2], bars[0][3]))
    n, d = bars[-1][2], bars[-1][3]
    size = n * 4 / d
    while bars[-1][0] + bars[-1][1] < last_beat + 1e-9:
        bars.append((bars[-1][0] + bars[-1][1], size, n, d))
    split = []
    for start, length, n, d in bars:
        pieces = max(1, math.ceil(length / MAX_DECODE_BAR - 1e-9))
        for k in range(pieces):
            split.append((start + k * length / pieces, length / pieces, n, d))
    return clock, split


class Constraint:
    def __init__(self, codec, bars, window=WINDOW, sigma=SIGMA):
        import numpy as np
        import torch
        self.np, self.torch = np, torch
        self.codec, self.bars = codec, bars
        self.starts = [b[0] for b in bars]
        self.window, self.sigma = window, sigma
        self.lengths = codec.tick_values['bar_length']
        frac = torch.as_tensor(codec.fractions).double() / 480.
        self.offsets = torch.arange(64).double()[:, None] + frac[None, :]   # quarter offsets inside a bar
        self.changed = 0
        self.widened = 0

    def bar_at(self, beat):
        from bisect import bisect_right
        return max(0, min(bisect_right(self.starts, beat + 1e-9) - 1, len(self.bars) - 1))

    def candidates(self, u, anchor, window):
        out = []
        for m in range(self.bar_at(u - window), self.bar_at(u + window) + 1):
            start, length, n, d = self.bars[m]
            rel = m - anchor + self.codec.bar_offset
            if 0 <= rel < self.codec.sizes[0]:
                out.append((m, rel, start, length, n, d))
        return out

    def signature(self, u, anchor):
        """Everything this note's constraint depends on, relative to the window."""
        return _digest([(m - anchor, round(start - u, 9), round(length, 9), n, d)
                        for m, _, start, length, n, d in self.candidates(u, anchor, self.window)])

    def bar_length_id(self, length):
        np = self.np
        ticks = round(length * 480)
        ok = np.flatnonzero(self.lengths >= ticks)
        return int(ok[np.argmin(self.lengths[ok])]) if len(ok) else int(np.argmax(self.lengths))

    def meter_ids(self, n, d):
        return min(max(int(n), 1), 64) - 1, (self.codec.denominators.index(d) if d in self.codec.denominators else 2)

    def choose(self, logits, anchor, input_id, u):
        torch = self.torch
        s = [torch.log_softmax(x[0, -1].float(), -1) for x in logits]
        free = torch.stack([v.argmax() for v in s])
        row = free.clone()
        base = s[1].double()[:, None] + s[2].double()[None, :]
        # The offset table is built once on the host; bind it to the actual
        # decoder logits once per device (CPU and CUDA keep the same values).
        if self.offsets.device != base.device:
            self.offsets = self.offsets.to(device=base.device)
        best = None
        for window in (self.window, 2 * self.window, 4 * self.window, 64.):
            for m, rel, start, length, n, d in self.candidates(u, anchor, window):
                pos = start + self.offsets
                ok = ((pos - start) < length - 1e-9) & ((pos - u).abs() <= window)
                if not bool(ok.any()):
                    continue
                score = (s[0][rel].double() + base - (pos - u) ** 2 / (2 * self.sigma ** 2)).masked_fill(~ok, -math.inf)
                k = int(score.flatten().argmax())
                value = float(score.flatten()[k])
                if best is None or value > best[0]:
                    best = (value, rel, k // score.shape[1], k % score.shape[1], length, n, d)
            if best is not None:
                self.widened += window > self.window
                break
        if best is None:
            raise ValueError(f'输入音符 {input_id} 在拍点图第 {u:.3f} 拍附近没有可写的位置')
        _, rel, qi, fi, length, n, d = best
        row[0], row[1], row[2] = rel, qi, fi
        row[5] = self.bar_length_id(length)
        row[6], row[7] = self.meter_ids(n, d)
        if row[10]:
            row[3] = 0
        else:
            dur = s[3].clone(); dur[0] = -math.inf; row[3] = dur.argmax()
        self.changed += int(row[1] != free[1] or row[2] != free[2])
        return row


class Decoder:
    """The pinned DuET model plus a per-window encoding cache (kept by the service)."""

    def __init__(self, request):
        sys.path.insert(0, str(Path(request['source']) / 'src'))
        import hashlib as _h
        if not hasattr(_h, 'file_digest'):
            def file_digest(fileobj, digest):
                result = _h.new(digest) if isinstance(digest, str) else digest()
                for chunk in iter(lambda: fileobj.read(1 << 20), b''):
                    result.update(chunk)
                return result
            _h.file_digest = file_digest
        import torch
        from duet import DuET
        torch.set_num_threads(int(request.get('threads', 8)))
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        self.torch = torch
        self.model = DuET.from_pretrained(request['model'], local_files_only=True,
                                          device=None if request['device'] == 'auto' else request['device'])
        self.net = self.model.model
        self.encodings = {}

    def encode(self, key, batch):
        hit = self.encodings.pop(key, None)
        if hit is None:
            memory, valid, _ = self.net.encode(batch)
            hit = (memory, valid, self.net.positions(batch, memory))
        self.encodings[key] = hit
        while len(self.encodings) > 48:
            self.encodings.pop(next(iter(self.encodings)))
        return hit

    def decode(self, batch, key, known_rows, anchor, constraint, queue, old_rows=None, stop_from=None):
        """DuET's cached step decoder (same math as generate_rows), with the
        known rows run as one batched pass and an optional early stop.

        Returns (new rows for ``queue``, number decoded, stopped_early).
        """
        torch, net = self.torch, self.net
        from duet.layers import apply_rotary
        import torch.nn.functional as F
        memory, valid, pos = self.encode(key, batch)
        slots = batch['query_note_slots']
        n = slots.shape[1]
        device = slots.device
        P = len(known_rows)
        if P + len(queue) != n:
            raise ValueError('DuET 约束解码音符数不一致')
        cache = [{} for _ in net.blocks]
        pitch = net.current_pitch(batch) if net.decoder_input_pitch else None
        width = net.codec.width
        rows = [torch.as_tensor(r, dtype=torch.long, device=device) for r in known_rows]
        ones = torch.ones(width, dtype=torch.bool, device=device)
        if P:
            # Step i consumes rows[i-1]; step 0 the BOS slot. One causal pass.
            ids = torch.zeros((1, P, width), dtype=torch.long, device=device)
            known = torch.zeros((1, P, width), dtype=torch.bool, device=device)
            if P > 1:
                ids[0, 1:] = torch.stack(rows[:P - 1]); known[0, 1:] = True
            context = torch.ones((1, P), dtype=torch.bool, device=device)
            x = net.embed(ids, known, context, first=True)
            if pitch is not None:
                x = x + pitch[:, :P]
            steps = torch.arange(P, device=device).float()[None]
            for j, block in enumerate(net.blocks):
                stream = net.decoder_stream(j)
                q, k = pos[stream]
                a = block.self_attention
                h = block.norms[0](x)
                qk = net.rotary(steps)
                query = apply_rotary(a._split(a.q_proj(h)), *qk)
                keys = apply_rotary(a._split(a.k_proj(h)), *qk)
                values = a._split(a.v_proj(h))
                cache[j].update(self_k=keys, self_v=values)
                y = F.scaled_dot_product_attention(query, keys, values, is_causal=True)
                x = x + a.o_proj(y.transpose(1, 2).reshape_as(x))
                x = x + block.cross(block.norms[1](x), memory[stream], valid[stream], net.cross_rotary(stream),
                                    q[:, :P], cache[j], k)
                x = x + block.ffn(block.norms[2](x))
        out, streak, decoded = [], 0, 0
        for offset, (input_id, u) in enumerate(queue):
            i = P + offset
            if i == 0:
                ids = torch.zeros((1, 1, width), dtype=torch.long, device=device)
                known = torch.zeros_like(ids, dtype=torch.bool)
            else:
                ids = rows[-1][None, None]; known = ones[None, None]
            context = torch.full((1, 1), False, device=device, dtype=torch.bool)
            x = net.embed(ids, known, context, first=i == 0)
            if pitch is not None:
                x = x + pitch[:, i:i + 1]
            for j, block in enumerate(net.blocks):
                stream = net.decoder_stream(j)
                q, k = pos[stream]
                x = block.step(x, memory[stream], valid[stream], net.rotary, cache[j], i,
                               q[:, i:i + 1], k, net.cross_rotary(stream))
            hidden = net.final_norm(x)
            row = constraint.choose([head(hidden) for head in net.heads], anchor, input_id, u)
            rows.append(row); decoded += 1
            values = [int(v) for v in row]
            out.append(values)
            if old_rows is not None and stop_from is not None:
                streak = streak + 1 if values == old_rows[offset] else 0
                if offset >= stop_from and streak >= STOP_AFTER:
                    return out + [list(r) for r in old_rows[offset + 1:]], decoded, True
        return out, decoded, False


def _context_known_rows(history, committed, anchor, codec):
    rows = []
    for i in history:
        row = list(committed[i]['row']); row[0] = committed[i]['bar'] - anchor + codec.bar_offset
        rows.append(row)
    return rows


def _run(request, emit, decoder_box):
    """Decode every window; reuse whole windows, known notes and unchanged tails."""
    sys.path.insert(0, str(Path(request['source']) / 'src'))
    import numpy as np
    from duet.codec import Codec
    from duet.config import CORE_SECONDS, MAX_NOTES
    from duet.features import build_window
    from duet.midi import read_performance_midi
    from duet.assembly import assemble_records
    from .timing import TempoMap
    midi = Path(request['input']); original = Path(request['original_cache']); out = Path(request['output'])
    input_sha = _hash(midi)
    if input_sha != request['input_sha256']:
        raise ValueError('源 MIDI 已改变')
    data = read_performance_midi(midi)
    on = data['ref_onset']; pitch = data['ref_pitch']; off = data['ref_offset']
    clock = TempoMap(json.loads(json.dumps(request['tempo_map'])))
    last = max(clock.to_beat(float(t)) for t in off) + 1.0
    clock, bars = decode_bars(request['tempo_map'], last)
    beat = [clock.to_beat(float(t)) for t in on]
    codec = Codec()
    constraint = Constraint(codec, bars)
    whole = {} if request.get('no_reuse') else request.get('reuse_windows', {})
    records_by_start = {} if request.get('no_reuse') else request.get('window_records', {})
    starts = list(np.arange(0.0, float(on.max()) + CORE_SECONDS, CORE_SECONDS))
    total = len({int(t // CORE_SECONDS) for t in on})
    records, committed, done, began = [], {}, 0, monotonic()
    stats = {'reused_windows': 0, 'decoded_notes': 0, 'known_notes': 0, 'spliced_notes': 0, 'early_stops': 0}
    windows, window_records = {}, {}
    emit({'completed_windows': 0, 'total_windows': total, 'elapsed_seconds': 0.0})
    for start in starts:
        new_ids = np.flatnonzero((on >= start) & (on < start + CORE_SECONDS))
        if not len(new_ids):
            continue
        batch, win_ids, _ = build_window(data, float(start))
        ids = [int(i) for i in win_ids]
        if len(ids) > MAX_NOTES:
            raise ValueError('DuET 窗口音符过多')
        history = [i for i in ids if on[i] < start]
        anchor = (min(committed[i]['bar'] for i in history) if history
                  else max((r['bar'] for r in records), default=0))
        owned = [i for i in ids if on[i] >= start]
        history_rows = _context_known_rows(history, committed, anchor, codec)
        note_sigs = [constraint.signature(beat[i], anchor) for i in owned]
        history_sig = _digest(history_rows)
        sig = _digest({'v': VERSION, 'start': float(start), 'owned': owned, 'h': history_sig, 'n': note_sigs})
        rows = whole.get(sig)
        if rows is not None and len(rows) == len(owned):
            rows = [list(r) for r in rows]
            stats['reused_windows'] += 1
        else:
            old = records_by_start.get(str(float(start)))
            if old and (old.get('owned') != owned or len(old.get('rows', [])) != len(owned)):
                old = None
            known = 0
            if old and old.get('history_sig') == history_sig:
                while known < len(owned) and old['note_sigs'][known] == note_sigs[known]:
                    known += 1
            if old and known == len(owned):
                rows = [list(r) for r in old['rows']]
                stats['reused_windows'] += 1
            else:
                if decoder_box.get('decoder') is None:
                    decoder_box['decoder'] = Decoder(request)
                decoder = decoder_box['decoder']
                batch = {k: v.to(decoder.model.device) for k, v in batch.items()}
                known_rows = history_rows + ([list(r) for r in old['rows'][:known]] if old else [])
                queue = [(i, beat[i]) for i in owned[known:]]
                old_tail, stop_from = None, None
                if old:
                    old_tail = old['rows'][known:]
                    differing = [k for k in range(known, len(owned)) if old['note_sigs'][k] != note_sigs[k]]
                    # Only splice once every changed note is behind us.
                    stop_from = (max(differing) - known) if differing else 0
                    if request.get('no_early_stop'):
                        old_tail = None
                new, decoded, stopped = decoder.decode(batch, (input_sha, float(start)), known_rows, anchor,
                                                       constraint, queue, old_tail, stop_from)
                rows = ([list(r) for r in old['rows'][:known]] if old else []) + new
                stats['decoded_notes'] += decoded
                stats['known_notes'] += known
                stats['spliced_notes'] += len(queue) - decoded
                stats['early_stops'] += int(stopped)
        windows[sig] = rows
        window_records[str(float(start))] = {'owned': owned, 'history_sig': history_sig,
                                             'note_sigs': note_sigs, 'rows': rows}
        for i, row in zip(owned, rows):
            r = {'input_id': i, 'pitch': int(pitch[i]), 'bar': int(row[0]) - codec.bar_offset + anchor, 'row': row}
            records.append(r); committed[i] = r
        done += 1
        emit({'completed_windows': done, 'total_windows': total, **stats, 'elapsed_seconds': monotonic() - began})
    if set(committed) != set(range(len(on))):
        raise ValueError('DuET 约束解码没有覆盖全部音符')
    score, assembly = assemble_records(codec, records)
    attrs = codec.attributes(np.array([r['row'] for r in records]))
    positions = [bars[r['bar']][0] + float(t) / 480 for r, t in zip(records, attrs['onset_tick'])]
    order = [r['input_id'] for r in records]
    if order != sorted(order) or not np.array_equal(score.notes[:, 0], pitch):
        raise ValueError('DuET 约束解码的音符顺序与输入不一致')
    out.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(out / 'score.npz', schema=np.asarray('apt_whole_score_v1'), notes=score.notes,
                        bar_starts=score.bar_starts, bar_lengths=score.bar_lengths,
                        meters=np.asarray(score.meters, dtype=str), voices=score.voices, keys=score.keys,
                        note_order=score.note_order, is_grace=score.notes[:, 2] == 0)
    (out / 'notes.json').write_text(json.dumps(records) + '\n', encoding='utf-8')
    shutil.copy2(original / 'frames.csv', out / 'frames.csv')
    source_prediction = _hash(original / 'score.npz')
    report = json.loads((original / 'report.json').read_text(encoding='utf-8'))
    report['constrained'] = {'version': VERSION, 'window_quarters': WINDOW, 'timing_sigma_quarters': SIGMA,
                             'bars': len(bars), 'decode_seconds': monotonic() - began, 'windows': done,
                             **stats, 'device': request['device'],
                             'notes_with_position_differing_from_free_choice': constraint.changed,
                             'notes_needing_wider_window': constraint.widened,
                             'source_prediction_sha256': source_prediction, **assembly}
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (out / 'windows.json').write_text(json.dumps({'version': VERSION, 'source_prediction_sha256': source_prediction,
                                                  'windows': windows, 'records': window_records}) + '\n', encoding='utf-8')
    (out / 'constrained.json').write_text(json.dumps({
        'version': VERSION, 'reference_sha256': request['reference_sha256'],
        'anchors_sha256': request['anchors_sha256'], 'source_prediction_sha256': source_prediction,
        'positions_quarters': positions,
        'durations_quarters': [float(v) for v in score.notes[:, 2]],
        'bar_starts': [b[0] for b in bars]}) + '\n', encoding='utf-8')
    identity = json.loads((original / 'astra-cache.json').read_text(encoding='utf-8'))['identity']
    manifest = {'identity': dict(identity, constrained_reference_sha256=request['reference_sha256'],
                                 constrained_version=VERSION),
                'artifacts': {name: _hash(out / name) for name in ARTIFACTS}}
    (out / 'astra-cache.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def _worker(request):
    """One-shot run (fallback when the resident service is unavailable)."""
    import torch
    with torch.inference_mode():
        _run(request, lambda event: print(json.dumps(event), flush=True), {})


# ---------------------------------------------------------------- service

def _code_version():
    return _hash(Path(__file__))


def _diagnose(where, exc):
    """Keep the last failure with its traceback for support (small, overwritten)."""
    try:
        import traceback
        from .release_paths import data_root
        (data_root() / '.cache' / 'duet-service-error.txt').write_text(
            f'{where}: ' + ''.join(traceback.format_exception(exc)), encoding='utf-8')
    except Exception:
        pass


def _service_file():
    from .release_paths import data_root
    return data_root() / '.cache' / 'duet-service.json'


def serve(state_file):
    """Keep DuET loaded; one request at a time over a token-protected localhost socket."""
    import secrets
    import torch
    token = secrets.token_hex(16)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0)); server.listen(4); server.settimeout(5.0)
    state = {'port': server.getsockname()[1], 'token': token, 'pid': os.getpid(), 'code': _code_version()}
    Path(state_file).parent.mkdir(parents=True, exist_ok=True)
    pending = Path(state_file).with_suffix('.tmp')
    pending.write_text(json.dumps(state), encoding='utf-8'); os.replace(pending, state_file)
    box, last_used = {}, monotonic()
    try:
        while monotonic() - last_used < SERVICE_IDLE_SECONDS:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                stream = conn.makefile('rwb')
                def send(obj):
                    stream.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')); stream.flush()
                try:
                    request = json.loads(stream.readline().decode('utf-8'))
                    if request.get('token') != token:
                        continue
                    if request.get('ping'):
                        send({'pong': True, 'code': state['code']}); continue
                    with torch.inference_mode():
                        _run(request['job'], lambda event: send({'event': event}), box)
                    send({'done': True})
                except (BrokenPipeError, ConnectionError, OSError):
                    pass   # client went away (cancelled); nothing is published
                except Exception as exc:  # report to the caller, keep serving
                    try:
                        send({'error': f'{type(exc).__name__}: {exc}'})
                    except OSError:
                        pass
                finally:
                    last_used = monotonic()
    finally:
        try:
            if json.loads(Path(state_file).read_text(encoding='utf-8')).get('pid') == os.getpid():
                Path(state_file).unlink()
        except (OSError, ValueError):
            pass


def _connect(state_file, timeout=3.0):
    try:
        state = json.loads(Path(state_file).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if state.get('code') != _code_version():
        return None
    try:
        conn = socket.create_connection(('127.0.0.1', int(state['port'])), timeout=timeout)
    except OSError:
        return None
    return conn, state


def _ping(state_file):
    got = _connect(state_file)
    if not got:
        return None
    conn, state = got
    try:
        with conn:
            conn.sendall((json.dumps({'token': state['token'], 'ping': True}) + '\n').encode())
            reply = json.loads(conn.makefile('rb').readline().decode() or '{}')
            return state if reply.get('pong') else None
    except (OSError, ValueError):
        return None


def _start_service(python, env, state_file, log):
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    command = [python, '-u', '-m', 'astra.duet_constrained', '--serve', str(state_file)]
    for extra in (getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0), 0):
        try:
            subprocess.Popen(command, env=env, creationflags=flags | extra, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            break
        except OSError:
            continue
    else:
        return None
    log('[duet] 启动常驻写谱服务（之后的更新不再重复加载模型）')
    began = monotonic()
    while monotonic() - began < 60:
        state = _ping(state_file)
        if state:
            return state
        threading.Event().wait(.3)
    return None


def _via_service(job, python, env, log, progress):
    state_file = _service_file()
    state = _ping(state_file) or _start_service(python, env, state_file, log)
    if not state:
        return False
    got = _connect(state_file, timeout=10.0)
    if not got:
        return False
    conn, state = got
    from .preview_cancel import check_cancelled
    with conn:
        conn.settimeout(None)
        conn.sendall((json.dumps({'token': state['token'], 'job': job}, ensure_ascii=False) + '\n').encode('utf-8'))
        reader = conn.makefile('rb')
        for raw in reader:
            check_cancelled()   # raising closes the socket; the service drops the job
            message = json.loads(raw.decode('utf-8'))
            if 'event' in message:
                progress(message['event'])
            elif message.get('done'):
                return True
            elif 'error' in message:
                raise RuntimeError('DuET 按拍点写谱失败：' + message['error'])
    raise RuntimeError('DuET 写谱服务意外断开')


# ---------------------------------------------------------------- caller

def reference_digest(tempo):
    from .duet_beat_map import map_digest
    return map_digest(tempo)


def _valid(folder):
    try:
        recorded = json.loads((folder / 'astra-cache.json').read_text(encoding='utf-8'))
        marker = json.loads((folder / 'constrained.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if marker.get('version') != VERSION or set(recorded.get('artifacts', {})) != set(ARTIFACTS):
        return None
    if not all((folder / n).is_file() and _hash(folder / n) == h for n, h in recorded['artifacts'].items()):
        return None
    return marker


def _prune(root, keep):
    folders = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.endswith('.pending')),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for folder in folders[keep:]:
        shutil.rmtree(folder, ignore_errors=True)


def constrained_prediction(midi, original_cache, tempo_map, project, *, options=None, log=print):
    """Return the cache directory DuET decoded on this beat map (cached)."""
    from .duet_score import duet_source_dir, runtime_env_root, ROOT, _run_prediction_logged
    from .duet_beat_map import anchors_digest
    from .model_worker_lease import windowless_python
    options = options or {}
    midi = Path(midi).resolve(strict=True); original = Path(original_cache).resolve(strict=True)
    reference = reference_digest(tempo_map)
    anchors = anchors_digest(tempo_map)
    source_prediction = _hash(original / 'score.npz')
    root = Path(project) / 'cache' / 'duet-constrained'
    root.mkdir(parents=True, exist_ok=True)
    known = [(folder, _valid(folder)) for folder in root.iterdir() if folder.is_dir()]
    known = [(folder, m) for folder, m in known if m and m.get('source_prediction_sha256') == source_prediction]
    # Same beat points as an earlier decode: positions are unchanged, only the
    # bar grouping differs. Reuse that decode instead of running DuET again.
    for folder, marker in known:
        if marker.get('anchors_sha256') == anchors:
            os.utime(folder)
            log('[duet] 拍点未变，只改了小节分组：沿用已写好的 DuET 谱重新分小节，不重跑模型')
            return folder
    key = hashlib.sha256(json.dumps({'v': VERSION, 'input': _hash(midi), 'prediction': source_prediction,
                                     'reference': reference, 'window': WINDOW, 'sigma': SIGMA},
                                    sort_keys=True).encode()).hexdigest()[:32]
    folder = root / key
    if folder.exists():
        shutil.rmtree(folder)
    whole, by_start = {}, {}
    for previous, _ in sorted(known, key=lambda item: item[0].stat().st_mtime):
        try:
            record = json.loads((previous / 'windows.json').read_text(encoding='utf-8'))
            whole.update(record.get('windows', {}))
            by_start.update(record.get('records', {}))
        except (OSError, ValueError):
            pass
    runtime = runtime_env_root(ROOT)
    device = options.get('duet_device', 'cpu')
    env_name = '.venv-score-gpu' if runtime != ROOT and device == 'cuda' else '.venv-score'
    python = windowless_python(options.get('python') or str(runtime / env_name / 'Scripts' / 'python.exe'))
    pending = folder.with_name(folder.name + '.pending')
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    job = {'source': str(duet_source_dir()), 'input': str(midi), 'input_sha256': _hash(midi),
           'original_cache': str(original), 'tempo_map': tempo_map, 'reference_sha256': reference,
           'anchors_sha256': anchors, 'reuse_windows': whole, 'window_records': by_start,
           'output': str(pending / 'out'), 'model': str(runtime / 'models' / 'duet'),
           'device': device, 'threads': min(8, os.cpu_count() or 4) if device == 'cpu' else int(options.get('threads', 2))}
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONIOENCODING='utf-8')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[1]), env.get('PYTHONPATH'))))
    if device == 'cpu':
        env['CUDA_VISIBLE_DEVICES'] = '-1'
    from .resource_admission import wait_for_model_admission
    wait_for_model_admission(device, 'DuET', log)
    log('[duet] 按当前拍点、拍号和小节让 DuET 重新写谱（只重写改动涉及的部分）')

    def progress(event):
        extra = []
        if event.get('reused_windows'):
            extra.append(f"{event['reused_windows']} 段未变")
        if event.get('known_notes') or event.get('spliced_notes'):
            extra.append(f"沿用 {event.get('known_notes', 0) + event.get('spliced_notes', 0)} 个音")
        log(f"[duet] 按拍点写谱 {event['completed_windows']}/{event['total_windows']} 段"
            + (f"（{'，'.join(extra)}）" if extra else ''))
    try:
        served = False
        if device == 'cpu' and not options.get('duet_no_service'):
            try:
                served = _via_service(job, python, env, log, progress)
            except (OSError, ValueError) as exc:
                _diagnose('service', exc)
                log(f'[duet] 常驻写谱服务不可用（{exc}），改为单次运行')
        if not served:
            (pending / 'request.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
            command = [python, '-u', '-m', 'astra.duet_constrained', str(pending / 'request.json')]

            def line_progress(line):
                text = line.removeprefix('[duet] ').strip()
                if text.startswith('{'):
                    try:
                        progress(json.loads(text)); return
                    except (ValueError, KeyError):
                        pass
                log(line)
            from .preview_cancel import run_logged_process
            # Explicit stdin: a windowed (pythonw) parent has no valid one.
            code, last_line = run_logged_process(command, line_progress, encoding='utf-8', errors='replace', env=env,
                                                 stdin=subprocess.DEVNULL, timeout=3600,
                                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if code:
                raise RuntimeError(f'DuET 按拍点写谱失败（退出码 {code}）：{last_line}')
        if _valid(pending / 'out') is None:
            raise RuntimeError('DuET 按拍点写谱结果不完整')
        (pending / 'out').replace(folder)
    except Exception as exc:
        _diagnose('constrained_prediction', exc)
        raise
    finally:
        shutil.rmtree(pending, ignore_errors=True)
    _prune(root, KEEP_CACHES)
    return folder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request', nargs='?')
    parser.add_argument('--serve')
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
    else:
        _worker(json.loads(Path(args.request).read_text(encoding='utf-8')))


if __name__ == '__main__':
    main()
