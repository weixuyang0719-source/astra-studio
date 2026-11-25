"""Direct, local PPQ MIDI import. No audio separation or beat estimation."""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path
import hashlib
import io

import mido

from . import pipeline
from .timing import TempoMap


def _load(path):
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in ('.mid', '.midi') or not source.is_file():
        raise ValueError('请选择存在的 .mid 或 .midi 文件')
    data = source.read_bytes()
    midi = mido.MidiFile(file=io.BytesIO(data))
    if midi.type not in (0, 1) or midi.ticks_per_beat <= 0:
        raise ValueError('仅支持同步 type 0/1、PPQ 时基的 MIDI；不支持 type 2 或 SMPTE 时基')
    return source, data, midi


def _events(midi):
    events = []
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        for order, message in enumerate(track):
            tick += message.time
            events.append((tick, track_index, order, message))
    return sorted(events, key=lambda item: item[:3])


def _tempo(midi, events):
    ppq = midi.ticks_per_beat
    tempos, meters = {0: 500000}, {0: (4, 4)}
    explicit_tempo = explicit_meter = False
    last_tick = max((e[0] for e in events), default=0)
    for tick, _, _, message in events:
        if message.type == 'set_tempo':
            if message.tempo <= 0:
                raise ValueError('MIDI 含无效速度事件')
            tempos[tick] = message.tempo
            explicit_tempo |= tick == 0
        elif message.type == 'time_signature':
            meters[tick] = (message.numerator, message.denominator)
            explicit_meter |= tick == 0
    warnings = []
    if not explicit_tempo:
        warnings.append('起点没有速度事件：按 MIDI 默认 120 BPM，后续已有变速事件保持不变。')
    if not explicit_meter:
        warnings.append('起点没有拍号事件：按 MIDI 默认 4/4，后续已有拍号保持不变。')
    anchors = [{'seconds': 0., 'beat': 0.}]
    previous_tick, seconds, micro = 0, 0., tempos[0]
    for tick, value in sorted(tempos.items()):
        if tick:
            seconds += (tick - previous_tick) / ppq * micro / 1e6
            anchors.append({'seconds': seconds, 'beat': tick / ppq})
        previous_tick, micro = tick, value
    # A terminal slope is needed even if the final event is a tempo change.
    terminal_tick = max(last_tick, previous_tick + ppq)
    if terminal_tick > previous_tick:
        anchors.append({'seconds': seconds + (terminal_tick - previous_tick) / ppq * micro / 1e6,
                        'beat': terminal_tick / ppq})
    result = {'version': 1, 'source': 'imported_midi', 'source_ppq': ppq,
              'anchors': anchors,
              'time_signatures': [{'beat': tick / ppq, 'numerator': nd[0], 'denominator': nd[1]}
                                  for tick, nd in sorted(meters.items())],
              'midi_tempo_events': [{'tick': tick, 'beat': tick / ppq, 'microseconds_per_quarter': value}
                                    for tick, value in sorted(tempos.items())],
              'warnings': warnings}
    clock = TempoMap(result)
    return result, clock.to_seconds(last_tick / ppq), last_tick, not explicit_tempo, not explicit_meter


def inspect_midi(path: str | Path) -> dict:
    """Read-only track/channel inventory. Channels and tracks are zero-based."""
    source, data, midi = _load(path)
    tempo, duration, end_tick, default_tempo, default_meter = _tempo(midi, _events(midi))
    tracks = []
    for index, track in enumerate(midi.tracks):
        channels = {}
        for message in track:
            if not hasattr(message, 'channel'):
                continue
            detail = channels.setdefault(message.channel, {'channel': message.channel, 'note_count': 0,
                       'programs': set(), 'is_drum': message.channel == 9})
            if message.type == 'program_change':
                detail['programs'].add(message.program)
            elif message.type == 'note_on' and message.velocity:
                detail['note_count'] += 1
        details = []
        for channel, item in sorted(channels.items()):
            programs = sorted(item['programs']) or [0]
            details.append({**item, 'programs': programs,
                            'is_piano': channel != 9 and all(0 <= p <= 7 for p in programs)})
        note_count = sum(item['note_count'] for item in details)
        tracks.append({'index': index, 'name': track.name or f'轨道 {index + 1}',
                       'note_count': note_count, 'channels': [item['channel'] for item in details],
                       'programs': [{'channel': item['channel'], 'program': program}
                                    for item in details for program in item['programs']],
                       'channel_details': details,
                       'has_drums': any(item['is_drum'] and item['note_count'] for item in details),
                       'recommended': bool(note_count) and all(item['is_piano'] for item in details if item['note_count'])})
    return {'source': str(source), 'source_sha256': hashlib.sha256(data).hexdigest(),
            'type': midi.type, 'ppq': midi.ticks_per_beat,
            'duration': duration, 'duration_beats': end_tick / midi.ticks_per_beat,
            'tracks': tracks, 'tempo_map': tempo, 'warnings': list(tempo['warnings']),
            'default_tempo': default_tempo, 'default_meter': default_meter}


def _track(events, end_tick, name):
    result = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
    previous = 0
    for tick, order, message in sorted(events, key=lambda item: (item[0], item[1])):
        result.append(message.copy(time=tick - previous))
        previous = tick
    result.append(mido.MetaMessage('end_of_track', time=max(0, end_tick - previous)))
    return result


def read_selected_midi(path: str | Path, *, tracks: list[int], channels: list[int] | None = None) -> dict:
    """Return exact tick/beat/second notes, CC64, clock, and selected MIDI bytes.

    CC64 on a separate automation track is included for the selected channels,
    because MIDI controllers act on channels, not track containers.
    """
    source, original, midi = _load(path)
    if not tracks or any(type(t) is not int or not 0 <= t < len(midi.tracks) for t in tracks):
        raise ValueError('请明确选择至少一个有效钢琴轨道（轨道索引从 0 开始）')
    chosen = sorted(set(tracks))
    if channels is not None and (not channels or any(type(c) is not int or not 0 <= c < 16 for c in channels)):
        raise ValueError('声道索引必须为 0–15，或留空选择所选轨道的非鼓声道')
    if channels is not None and 9 in channels:
        raise ValueError('声道 10 是 GM 鼓声道，当前 DuET 钢琴制谱不能处理鼓轨')
    allowed = set(channels) if channels is not None else {
        msg.channel for i in chosen for msg in midi.tracks[i]
        if msg.type == 'note_on' and msg.velocity and msg.channel != 9}
    events = _events(midi)
    tempo, duration, end_tick, _, _ = _tempo(midi, events)
    clock, ppq = TempoMap(tempo), midi.ticks_per_beat
    warnings = list(tempo['warnings'])
    if any(msg.type == 'note_on' and msg.velocity and msg.channel == 9 for i in chosen for msg in midi.tracks[i]):
        warnings.append('所选轨道的 GM 鼓声道已排除；当前仅生成钢琴谱。')
    if any(msg.type == 'program_change' and msg.channel in allowed and msg.program > 7
           for i in chosen for msg in midi.tracks[i]):
        warnings.append('选定声部含非钢琴 GM 音色；将按你的选择作为钢琴制谱，不执行多乐器改编。')
    pending = defaultdict(deque)
    notes, controls, pedals = [], [], []
    holds = {}
    track_events = defaultdict(list)
    conductor = []
    for tick, track, order, message in events:
        if message.is_meta:
            if message.type in ('set_tempo', 'time_signature', 'key_signature', 'marker'):
                conductor.append((tick, track * 1000000 + order, message))
            elif track in chosen and message.type not in ('track_name','end_of_track'):
                track_events[track].append((tick,order,message))
            continue
        if not hasattr(message, 'channel') or message.channel not in allowed:
            continue
        keep = track in chosen or message.type == 'control_change' and message.control == 64
        if not keep:
            continue
        track_events[track].append((tick, order, message))
        if message.type == 'note_on' and message.velocity:
            pending[(track, message.channel, message.note)].append((tick, message.velocity))
        elif message.type in ('note_on', 'note_off'):
            queue = pending[(track, message.channel, message.note)]
            if not queue:
                raise ValueError(f'轨道 {track + 1} 的音符 {message.note} 存在无起点的 note-off，请先在DAW修复')
            start, velocity = queue.popleft()
            if tick <= start:
                raise ValueError('MIDI 含零时长音符，请先在DAW修复')
            notes.append({'pitch': message.note, 'velocity': velocity,
                          'start': clock.to_seconds(start / ppq), 'end': clock.to_seconds(tick / ppq),
                          'start_beat': start / ppq, 'end_beat': tick / ppq,
                          'start_tick': start, 'end_tick': tick, 'track': track, 'channel': message.channel})
        elif message.type == 'control_change' and message.control == 64:
            controls.append({'number': 64, 'value': message.value, 'tick': tick, 'beat': tick / ppq,
                             'seconds': clock.to_seconds(tick / ppq), 'channel': message.channel, 'track': track})
            if message.value >= 64 and message.channel not in holds:
                holds[message.channel] = tick
            elif message.value < 64 and message.channel in holds:
                start = holds.pop(message.channel)
                if tick > start:
                    pedals.append({'number': 64, 'channel': message.channel,
                                   'start': clock.to_seconds(start / ppq), 'end': clock.to_seconds(tick / ppq),
                                   'start_beat': start / ppq, 'end_beat': tick / ppq})
    if any(pending.values()):
        raise ValueError('MIDI 含缺少 note-off 的音符，请先在DAW修复；不会猜测结束位置')
    if not notes:
        raise ValueError('选定轨道/声道没有可制谱的钢琴音符')
    for channel, start in holds.items():
        if end_tick > start:
            pedals.append({'number': 64, 'channel': channel, 'start': clock.to_seconds(start / ppq),
                           'end': duration, 'start_beat': start / ppq, 'end_beat': end_tick / ppq})
        warnings.append(f'声道 {channel + 1} 的踏板在文件结束仍按下；原始CC保留，区间预览止于文件末尾。')
    selected = mido.MidiFile(type=1, ticks_per_beat=ppq, charset=midi.charset)
    selected.tracks.append(_track(conductor, end_tick, 'Astra / original MIDI clock'))
    for track, messages in sorted(track_events.items()):
        selected.tracks.append(_track(messages, end_tick, midi.tracks[track].name or f'Track {track + 1}'))
    output = io.BytesIO()
    selected.save(file=output)
    return {'source': str(source), 'source_bytes': original, 'selected_bytes': output.getvalue(),
            'source_sha256': hashlib.sha256(original).hexdigest(),
            'tracks': chosen, 'channels': sorted(allowed), 'notes': sorted(notes, key=lambda n: (n['start_tick'], n['pitch'])),
            'pedals': pedals, 'control_events': controls, 'tempo_map': tempo,
            'duration': duration, 'ppq': ppq, 'warnings': warnings}


def import_midi_project(path: str | Path, output_root: str | Path, *, tracks: list[int],
                        channels: list[int] | None = None, config: dict | None = None,
                        mode: str = 'all', log=print) -> Path:
    """Create/reuse a normal Astra project, then run only score/export stages."""
    if mode not in ('all', 'midi', 'score'):
        raise ValueError('输出模式须为 all/midi/score')
    log('读取 MIDI：保留原速度、拍号与起始空白')
    imported = read_selected_midi(path, tracks=tracks, channels=channels)
    digest = hashlib.sha256(imported['selected_bytes']).hexdigest()
    source = Path(imported['source'])
    project = Path(output_root).expanduser().resolve() / f'{source.stem[:55]}-midi-{digest[:12]}'
    project.mkdir(parents=True, exist_ok=True)
    config = deepcopy(config if config is not None else pipeline.load_config())
    config['score'] = {'piano_engine': 'duet', **config.get('score', {})}
    with pipeline.project_lock(project):
        original = project / 'source.mid'
        selected = project / 'selected_source.mid'
        # Content-addressed selected input is immutable; UI options/tempo edits
        # do not invalidate DuET's MIDI-byte cache.
        if not selected.exists():
            selected.write_bytes(imported['selected_bytes'])
        elif pipeline.fingerprint(selected) != digest:
            raise RuntimeError('项目选定MIDI缓存被修改，请使用新输出目录重新导入')
        if not original.exists() or pipeline.fingerprint(original) != imported['source_sha256']:
            original.write_bytes(imported['source_bytes'])
        cache_key = digest[:12]
        bundle_path = pipeline._notes_path(project, 'piano', cache_key)
        bundle = {name: imported[name] for name in ('notes', 'pedals', 'control_events')}
        if not bundle_path.exists():
            pipeline.save_json(bundle_path, bundle)
        else:
            log('复用 MIDI 音符缓存；制谱模型按输入内容缓存')
        manifest = {name: imported[name] for name in
                    ('source', 'source_sha256', 'tracks', 'channels', 'duration', 'ppq', 'warnings')}
        manifest['selected_sha256'] = digest
        pipeline.save_json(project / 'midi_import.json', manifest)
        tempo = imported['tempo_map']
        tempo['warnings'] = imported['warnings']
        pipeline.save_json(project / 'source_tempo_map.json', tempo)
        state = {'version': 2, 'source_kind': 'midi', 'source': str(source),
                 'source_sha256': imported['source_sha256'], 'original_midi': str(original),
                 'selected_midi': str(selected), 'duration': imported['duration'],
                 'instruments': ['piano'], 'config': config, 'keep_stems': False,
                 'analysis_keys': {'piano': cache_key}, 'tempo_kind': 'imported_midi', 'stem_keys': {},
                 'midi_selection': {'tracks': imported['tracks'], 'channels': imported['channels'], 'ppq': imported['ppq']}}
        log('跳过音频分轨、转录和节拍检测；使用选定 MIDI 钢琴声部')
        export = pipeline._export(project, state, tempo, mode, log)
        return pipeline._commit_export(project, state, tempo, export, log)
