"""Encode the native editor's immutable playback view for VST RT v1.

The snapshot supplies performance seconds and the effective tempo anchors.
Bar boundaries come from the existing native document query, on a detached
read of that same Frozen. Neither a GUI tempo draft nor an SMF is an input.
"""
from bisect import bisect_right
from math import isfinite


def sample_of(seconds, rate):
    if type(seconds) not in (float, int) or not isfinite(seconds) or seconds < 0:
        raise ValueError('无效的 VST 时间位置。')
    return round(seconds * rate)


def _uint(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('无效的 MIDI 数据。')
    return value


def native_clock_points(frozen, data, rate, duration_samples):
    # Stateless native queries consume only this retained snapshot's authority.
    # Bar geometry, pickup and shortened-bar policy are never reconstructed here.
    from astra import document_editor as editor
    tempo, duration = data['tempo'], data['duration']
    reader = editor.detached(frozen.document())
    def query(name, **fields):
        return reader.run(editor.QUERY, reader.token(), dict(query=name, args=fields, base_revision=frozen.head['revision']))
    try:
        geometry = query('geometry', strict=False, ignore_score_end=True)
        starts = geometry['starts']
        seconds = query('to_seconds_many', beats=starts)
        bars = [dict(beat=q, seconds=t) for q, t in zip(starts, seconds)]
        meters = tempo['time_signatures']
        meter_times = query('to_seconds_many', beats=[m['beat'] for m in meters])
        anchors = tempo['anchors']
        times = [a['seconds'] for a in anchors]
        bar_times = [row['seconds'] for row in bars]
        boundaries = sorted({0., *times, *meter_times, *bar_times})
        boundaries = [t for t in boundaries if 0 <= t <= duration and sample_of(t, rate) < duration_samples]
        beats = query('to_beats', seconds=boundaries)
        out = {}
        for t, beat in zip(boundaries, beats):
            i = min(len(anchors)-2, max(0, bisect_right(times, t)-1))
            left, right = anchors[i:i+2]
            # Encoding a native linear anchor span as VST's constant tempo span.
            bpm = 60*(right['beat']-left['beat'])/(right['seconds']-left['seconds'])
            j = max(0, bisect_right(meter_times, t)-1)
            meter = meters[j]
            k = bisect_right(bar_times, t)-1
            bar_beat = bars[k]['beat'] if k >= 0 else 0.
            valid = not bool(meter.get('free'))
            out[sample_of(t, rate)] = dict(sample=sample_of(t, rate), beat=beat, bpm=bpm,
                numerator=meter['numerator'], denominator=meter['denominator'],
                bar_start_beat=bar_beat, meter_valid=valid)
        return list(out.values())
    finally:
        reader.close()


def native_click_points(frozen, data, rate, duration_samples):
    from astra import document_editor as editor
    reader = editor.detached(frozen.document())
    def query(name, **fields):
        return reader.run(editor.QUERY, reader.token(), dict(query=name,args=fields,base_revision=frozen.head['revision']))
    try:
        pulse = query('pulse_state')
        geometry = query('geometry',strict=False,ignore_score_end=True)
        starts = geometry['starts']
        meters = pulse['time_signatures']; meter_beats = [m['beat'] for m in meters]
        explicit = 'pulse_points' in data['tempo'] or pulse.get('pulse_migration_source') == 'model_projection'
        events = {}
        for point in pulse['pulse_points']:
            seconds, beat = point['seconds'], point['beat']
            if not 0 <= seconds < data['duration']:
                continue
            frame = sample_of(seconds,rate)
            if frame >= duration_samples:
                continue
            meter = meters[max(0,bisect_right(meter_beats,beat)-1)]
            free = bool(meter.get('free'))
            if free and not explicit:
                continue
            i = bisect_right(starts,beat+1e-8)-1
            accent = not free and i >= 0 and abs(starts[i]-beat) <= 1e-8
            events[frame] = events.get(frame,False) or accent
        if len(events)>100000:
            raise ValueError('VST click event budget exceeded')
        return [dict(sample=frame,accent=accent) for frame,accent in sorted(events.items())]
    finally:
        reader.close()


class Projector:
    """One projection in flight per route; cache clock work across note edits."""
    def __init__(self, clock_builder=native_clock_points, click_builder=native_click_points):
        self.clock_builder = clock_builder
        self.click_builder = click_builder
        self.cached = None

    def __call__(self, frozen, epoch, rate):
        head, data = frozen.head, frozen.playback()
        if any(data[key] != head[key] for key in ('project_id', 'stamp', 'source_key', 'applied_sequence')):
            raise ValueError('VST 冻结快照身份不匹配。')
        duration = data['duration']
        extent = sample_of(duration, rate)
        rows, warnings = [], []
        import mido
        for event in data.get('other_events') or ():
            fields = dict(event['message'])
            if fields.get('type') in ('set_tempo', 'time_signature', 'end_of_track'):
                continue
            try:
                msg = mido.Message.from_dict(fields)
            except LookupError:
                mido.MetaMessage.from_dict(fields)
                continue
            if msg.type not in ('polytouch', 'program_change', 'aftertouch', 'pitchwheel'):
                warnings.append('VST cannot send preserved '+msg.type+'; source event retained')
                continue
            raw = msg.bytes()
            rows.append((event['seconds'], 0, raw[0], raw[1], raw[2] if len(raw)>2 else 0))
            if msg.type == 'program_change':
                warnings.append('VST program changes are not mapped by this host; source event retained')
        for c in data['controls']:
            rows.append((c['seconds'], 0, 0xb0 | _uint(c['channel'], 15),
                         _uint(c['number'], 127), _uint(c['value'], 127)))
        for n in data['notes']:
            a, b = n['start'], n['end']
            ch = 0 if n.get('channel') is None else _uint(n['channel'], 15)
            pitch, velocity = _uint(n['pitch'], 127), _uint(n['velocity'], 127)
            if b <= a or not velocity:
                raise ValueError('无效的 VST 音符时值或力度。')
            rows.extend(((a, 2, 0x90 | ch, pitch, velocity), (b, 1, 0x80 | ch, pitch, 0)))
        # Stable source order within each group. At exactly equal seconds use
        # CC, release, attack, as in the working MIDI lane. At distinct times
        # that round to the same frame retain chronology (including tiny notes).
        rows.sort(key=lambda r: (r[0], r[1]))
        events = [dict(sample=sample_of(t, rate), status=s, data1=a, data2=b) for t, _, s, a, b in rows]
        # The native host schedules in frames, not unrounded float seconds.
        # MIDI duration and final messages can use different summation paths;
        # retain a boundary release without admitting events on later frames.
        if events and events[-1]['sample'] > extent:
            raise ValueError('VST 快照事件超出工程时长。')
        extent = max(1, extent, events[-1]['sample']+1 if events else 0)
        key = (data['tempo'], duration, rate, extent)
        if self.cached is None or key != self.cached[0]:
            clock = self.clock_builder(frozen, data, rate, extent)
            self.cached = (key, clock, self.click_builder(frozen,data,rate,extent))
        clicks=self.cached[2]
        review=getattr(frozen,'review_click_table',None)
        if review is not None:
            frames={}
            for second,accent in review:
                frame=sample_of(second,rate)
                if frame < extent:
                    frames[frame]=frames.get(frame,False) or accent
            clicks=[dict(sample=frame,accent=accent) for frame,accent in sorted(frames.items())]
        # Source events stay in the document. Do not synthesize program
        # changes, expand pedal into note lengths, or guess mapped parameters.
        return dict(version=1, project_id=head['project_id'], epoch=epoch,
                    sequence=head['applied_sequence'], sample_rate=rate,
                    duration_samples=extent, events=events, clock=self.cached[1], clicks=clicks, warnings=list(dict.fromkeys(warnings)))
