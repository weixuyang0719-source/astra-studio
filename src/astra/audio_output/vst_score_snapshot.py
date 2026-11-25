"""Decode bounded bytes produced by the trusted live MuseScore owner.

No project path is read or written. This is a technical checkpoint, not a
transport source selector: rack/seek/loop and meter alignment belong to Astra.
"""
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from heapq import merge

import mido


@dataclass(frozen=True, slots=True)
class PlaybackMessage:
    at: Fraction
    status: int
    data1: int
    data2: int


@dataclass(frozen=True, slots=True)
class PlaybackTempo:
    at: Fraction
    beat: Fraction
    microseconds_per_quarter: int


@dataclass(frozen=True, slots=True)
class PlaybackMeter:
    at: Fraction
    beat: Fraction
    numerator: int
    denominator: int


@dataclass(frozen=True, slots=True)
class ScorePlayback:
    duration: Fraction
    messages: tuple[PlaybackMessage, ...]
    tempo: tuple[PlaybackTempo, ...]
    meter: tuple[PlaybackMeter, ...]
    clicks: tuple[tuple[Fraction,bool,int], ...] = ()


def _absolute(track):
    tick = 0
    for msg in track:
        if type(msg.time) is not int or msg.time < 0:
            raise ValueError("Invalid native score playback delta")
        tick += msg.time
        yield tick, msg


def flatten_tempo(raw: bytes) -> bytes:
    """Ignore score tempo: every later tempo marking takes the opening tempo.

    The rewritten file is what the audio, bar map, written-tick map, seek and
    metronome all decode, so every timeline stays consistent at one tempo.
    """
    if type(raw) is not bytes or not raw.startswith(b'MThd'):
        return raw
    smf = mido.MidiFile(file=BytesIO(raw), clip=False)
    first = None
    for _tick, msg in merge(*(_absolute(track) for track in smf.tracks), key=lambda row: row[0]):
        if msg.type == 'set_tempo':
            first = msg.tempo
            break
    if first is None:
        return raw
    changed = False
    for track in smf.tracks:
        for i, msg in enumerate(track):
            if msg.type == 'set_tempo' and msg.tempo != first:
                track[i] = msg.copy(tempo=first)
                changed = True
    if not changed:
        return raw
    out = BytesIO()
    smf.save(file=out)
    return out.getvalue()


def decode_live_score(raw: bytes) -> ScorePlayback:
    if type(raw) is not bytes or not 14 < len(raw) <= 700 * 1024 or not raw.startswith(b'MThd'):
        raise ValueError("Invalid native score playback packet")
    smf = mido.MidiFile(file=BytesIO(raw), clip=False)
    tpq = smf.ticks_per_beat
    if smf.type not in (0, 1) or not 1 <= tpq <= 32767 or not 1 <= len(smf.tracks) <= 16:
        raise ValueError("Unsupported piano playback timeline")
    tick, at, bpm_us = 0, Fraction(0), 500_000
    messages, tempo, meter = [], {0: PlaybackTempo(at, at, bpm_us)}, {0: PlaybackMeter(at, at, 4, 4)}
    # mido's merge_tracks copies every message twice (absolute then
    # relative). The bounded input tracks already have monotonic deltas.
    # Merge by integer tick with the same stable track order. Do integer
    # accumulation over a shared denominator and create Fraction only
    # for events that survive, not for metadata and discarded mappings.
    total, divisor = 0, tpq * 1_000_000
    last_tick = 0
    for tick, msg in merge(*(_absolute(track) for track in smf.tracks), key=lambda row: row[0]):
        total += (tick - last_tick) * bpm_us
        last_tick = tick
        if msg.type == "set_tempo":
            bpm_us = msg.tempo
            if not 1 <= bpm_us <= 0xffffff:
                raise ValueError("Invalid native score tempo")
            tempo[tick] = PlaybackTempo(Fraction(total, divisor), Fraction(tick, tpq), bpm_us)
        elif msg.type == "time_signature":
            # MuseScore writes any notated meter (transcriptions produce e.g. 39/4).
            # 64 matches the bar map, metronome and transport beat field limits.
            if not 1 <= msg.numerator <= 64 or msg.denominator not in (1,2,4,8,16,32,64,128):
                raise ValueError("Unsupported native score meter")
            meter[tick] = PlaybackMeter(Fraction(total, divisor), Fraction(tick, tpq), msg.numerator, msg.denominator)
        elif msg.type in ("note_on", "note_off", "control_change", "pitchwheel", "polytouch", "aftertouch"):
            if msg.type == 'control_change' and msg.control in (0, 32):
                continue
            b = msg.bytes()
            messages.append(PlaybackMessage(Fraction(total, divisor),b[0],b[1],b[2] if len(b)>2 else 0))
        # Generated SMF program/bank mappings cannot replace the mounted piano.
        # Program messages are excluded; channel messages otherwise use native order.
    at = Fraction(total, divisor)
    if at <= 0 or not messages:
        raise ValueError("Native score has nothing to play")
    return ScorePlayback(at, tuple(messages), tuple(tempo.values()), tuple(meter.values()))


def score_events_for_rate(score: ScorePlayback, rate: int) -> tuple[int, list]:
    if type(rate) is not int or not 8_000 <= rate <= 384_000:
        raise ValueError("Invalid native playback sample rate")
    events = [dict(sample=round(m.at*rate), status=m.status, data1=m.data1, data2=m.data2) for m in score.messages]
    extent = max(1, round(score.duration*rate), events[-1]["sample"]+1)
    return extent, events

@dataclass(frozen=True, slots=True)
class WireNotes:
    """Stable wire sequence for genuine Notes snapshots after a source switch."""
    original: object
    sequence: int

    @property
    def head(self):
        return dict(self.original.head,applied_sequence=self.sequence)

    def document(self):
        return self.original.document()

    def playback(self):
        return dict(self.original.playback(),applied_sequence=self.sequence)


@dataclass(frozen=True, slots=True)
class WireReviewNotes:
    original: object
    sequence: int
    review_click_table: tuple

    @property
    def head(self):
        return dict(self.original.head, applied_sequence=self.sequence)

    def document(self):
        return self.original.document()

    def playback(self):
        return dict(self.original.playback(), applied_sequence=self.sequence)


@dataclass(frozen=True, slots=True)
class WireScore:
    project: str
    sequence: int
    score: ScorePlayback

    @property
    def head(self):
        return dict(project_id=self.project,applied_sequence=self.sequence,duration=float(self.score.duration))

    def project_wire(self, _notes_projector, _frozen, epoch, rate):
        duration, events = score_events_for_rate(self.score,rate)
        # SMF timing is the actual VST clock; beat clicks use the separate
        # native measure geometry, not an inferred MIDI meter grid.
        by_sample={}
        timeline = sorted([(m.at, 0, m) for m in self.score.meter]+[(t.at, 1, t) for t in self.score.tempo])
        tempo=self.score.tempo[0]; meter=self.score.meter[0]
        for q,field,row in timeline:
            tempo, meter = (row,meter) if field else (tempo,row)
            sample=round(q*rate)
            if sample<duration:
                by_sample[sample]=dict(sample=sample,beat=float(row.beat),
                    bpm=60_000_000/tempo.microseconds_per_quarter,
                    numerator=meter.numerator,denominator=meter.denominator,
                    bar_start_beat=float(meter.beat),meter_valid=False)
        return dict(version=1,project_id=self.project,epoch=epoch,sequence=self.sequence,
                    sample_rate=rate,duration_samples=duration,events=events,
                    clock=list(by_sample.values()),
                    clicks=[dict(sample=round(at*rate),accent=accent,rateGate=gate)
                            for at,accent,gate in self.score.clicks if round(at*rate)<duration],
                    warnings=[])
