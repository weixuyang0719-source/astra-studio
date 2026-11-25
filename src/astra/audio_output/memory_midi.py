"""Applied document -> short MIDI words -> existing native scheduler.

Only the owner GUI thread reads the provider. Only the MIDI control worker
packs a score, opens WinMM, or submits a copied timeline. The GUI may raise
an atomic native fence; no Python callback enters the scheduler or PCM core.
"""
from dataclasses import dataclass
import json
import math
import threading
import time
from typing import Protocol

from .native_midi import Click, Event, MemoryPlan, NativeMidiSession, _LIVE

_REPLACE, _PLAY, _LOOP, _CLICK, _RESTART = 1, 2, 4, 8, 16
_CLOSE_FENCE = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class NoteRow:
    start: float
    end: float
    pitch: int
    velocity: int
    channel: int | None = None


@dataclass(frozen=True, slots=True)
class ControlRow:
    seconds: float
    channel: int
    number: int
    value: int


@dataclass(frozen=True, slots=True)
class ChannelWord:
    seconds: float
    packed: int


@dataclass(frozen=True, slots=True, eq=False)
class AppliedMidi:
    epoch: int
    sequence: int
    notes: tuple[NoteRow, ...]
    controls: tuple[ControlRow, ...]
    tempo: bytes | None
    duration: float
    other: tuple[ChannelWord, ...] = ()
    default_program: int = 0


@dataclass(frozen=True, slots=True)
class MidiHead:
    snapshot: AppliedMidi | None
    pending: bool = False


class SnapshotProvider(Protocol):
    def __call__(self) -> MidiHead: ...


class _Outdated(Exception):
    pass


def _u(n, limit):
    if type(n) is not int or not 0 <= n <= limit:
        raise ValueError('invalid MIDI integer')
    return n


def seconds(t, duration=float('inf')):
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= duration:
        raise ValueError('invalid MIDI position or duration')
    return float(t)


def _word(status, ch, a, b=0):
    return status | ch | (a << 8) | (b << 16)


@dataclass(frozen=True, slots=True)
class _Projection:
    score: AppliedMidi
    events: object
    clicks: object
    click_error: str | None


def pack_score(score, cancelled=lambda: False):
    duration = seconds(score.duration)
    default = _u(score.default_program, 127)
    rows = []
    programmed = set()

    def check(i):
        if i % 256 == 0 and cancelled():
            raise _Outdated()

    # Match the writer's non-note ordering: preserved messages then raw CC at
    # the same time, then note-offs, then attacks. No duplicate pedal layer.
    for i, row in enumerate(score.other):
        check(i)
        m = _u(row.packed, 0xffffffff)
        kind = m & 0xf0
        if kind not in (0xa0, 0xc0, 0xd0, 0xe0) or m & 0xff808000 or (kind in (0xc0, 0xd0) and m & 0xff0000):
            raise ValueError('expected a non-note, non-CC MIDI channel message')
        if kind == 0xc0:
            programmed.add(m & 15)
        rows.append((seconds(row.seconds, duration), 1, m))
    for i, row in enumerate(score.controls):
        check(i)
        rows.append((seconds(row.seconds, duration), 1,
                     _word(0xb0, _u(row.channel, 15), _u(row.number, 127), _u(row.value, 127))))
    melodic = tuple(ch for ch in range(16) if ch != 9)
    note_channels = set()
    until = [[-1.] * 16 for _ in range(128)]
    ordered = []
    for i, note in enumerate(score.notes):
        check(i)
        ordered.append((seconds(note.start, duration), note))
    ordered.sort(key=lambda item: item[0])
    for i, (a, note) in enumerate(ordered):
        check(i)
        b = seconds(note.end, duration)
        pitch, velocity = _u(note.pitch, 127), _u(note.velocity, 127)
        if b <= a or velocity == 0:
            raise ValueError('invalid effective MIDI note span')
        ch = note.channel
        if ch is None:
            ch = next((k for k in melodic if until[pitch][k] <= a), None)
            if ch is None:
                raise ValueError('same-pitch overlap exceeds the 15 automatic melodic channels')
        else:
            _u(ch, 15)  # Explicit overlap and channel 9 are preserved.
        until[pitch][ch] = max(until[pitch][ch], b)
        note_channels.add(ch)
        rows.extend(((a, 3, _word(0x90, ch, pitch, velocity)), (b, 2, _word(0x80, ch, pitch))))
    rows.extend((0., -1, _word(0xc0, ch, default)) for ch in sorted(note_channels - programmed - {9}))
    if len(rows) > 10000000:
        raise ValueError('native MIDI event budget exceeded')
    rows.sort(key=lambda row: (row[0], row[1]))
    if cancelled():
        raise _Outdated()
    events = (Event * len(rows))()
    for i, (at, _, word) in enumerate(rows):
        check(i)
        events[i] = Event(at, word, 0)
    beats = ()
    click_error = None
    if score.tempo is not None and duration:
        try:
            from .native_metronome import metronome_beats
            beats = tuple(metronome_beats(json.loads(score.tempo), duration))
            if len(beats) > 100000:
                raise ValueError('native click event budget exceeded')
        except Exception as exc:
            beats = ()
            click_error = str(exc) or type(exc).__name__
    elif score.tempo is None:
        click_error = '当前编辑会话没有节拍器速度图。'
    if cancelled():
        raise _Outdated()
    clicks = (Click * len(beats))(*(Click(at, int(accent), 0) for at, accent in beats))
    return _Projection(score, events, clicks, click_error)


@dataclass(frozen=True, slots=True, eq=False)
class Goal:
    score: AppliedMidi | None
    gate: int = 1
    locator: int = 1
    position: float = 0.
    playing: bool = False
    restart: bool = False
    rate: float = 1.
    gain: float = 1.
    loop: tuple = (0., 0., False)
    click: bool = False


@dataclass(frozen=True, slots=True)
class Sample:
    goal: Goal | None = None
    position: float = 0.
    playing: bool = False
    sampled_at: float | None = None
    error: str | None = None
    bad_score: AppliedMidi | None = None
    click_error: str | None = None
    closed: bool = False
    output_released: bool = False


def _native_plan(goal, projection, replace_score):
    duration = projection.score.duration
    a, b, loop = goal.loop
    flags = ((_REPLACE if replace_score else 0) | (_PLAY if goal.playing else 0)
             | (_LOOP if loop else 0) | (_CLICK if goal.click and not projection.click_error else 0)
             | (_RESTART if goal.restart else 0))
    return MemoryPlan(size=__import__('ctypes').sizeof(MemoryPlan), version=1,
                      gate=goal.gate, score=projection.score.sequence, locator=goal.locator,
                      position=goal.position, duration=duration, rate=goal.rate, gain=goal.gain,
                      left=min(a, duration), right=min(b, duration), flags=flags)


class MemoryMidiOwner:
    """One endpoint borrower. One projection in progress and one latest goal.

    A GUI update does not wait for a receipt (or for a blocked WinMM driver).
    The only foreign call under the lock is an atomic fence raise and wake.
    """
    def __init__(self, goal, factory=None):
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._wake = threading.Event()
        self._goal, self._sample, self._session = goal, Sample(), None
        self._factory = factory or NativeMidiSession
        _LIVE[id(self)] = self
        threading.Thread(target=self._run, name='Memory MIDI control', daemon=True).start()

    def snapshot(self):
        with self._lock:
            return self._sample

    def put(self, goal):
        with self._lock:
            if self._quit.is_set():
                return
            if self._session is not None and self._goal.gate != goal.gate:
                self._session.memory_fence(goal.gate)
            self._goal = goal
            self._wake.set()

    def close(self):
        with self._lock:
            self._quit.set()
            if self._session is not None:
                self._session.memory_fence(_CLOSE_FENCE)
            self._wake.set()

    def _current(self):
        with self._lock:
            return self._goal

    def _run(self):
        session = None
        fatal = None
        handled = installed = cached = bad = None
        bad_message = None
        try:
            if self._quit.is_set():
                return
            session = self._factory(memory=True)
            with self._lock:
                self._session = session
                session.memory_fence(_CLOSE_FENCE if self._quit.is_set() else self._goal.gate)
            while not self._quit.is_set():
                self._wake.clear()
                goal = self._current()
                score = goal.score
                if score is not None and goal is not handled and score is not bad:
                    if cached is None or cached.score is not score:
                        try:
                            cached = pack_score(score, lambda: self._quit.is_set() or self._current().score is not score)
                        except _Outdated:
                            continue
                        except Exception as exc:
                            bad, bad_message = score, str(exc) or type(exc).__name__
                    # Packing controls no transport. Read the latest locator,
                    # gate and requested Play *after* the expensive work.
                    goal = self._current()
                    if self._quit.is_set():
                        break
                    if goal.score is not score:
                        continue
                    if score is not bad:
                        replace_score = installed is not score
                        plan = _native_plan(goal, cached, replace_score)
                        seq = session.memory_submit(plan, cached.events if replace_score else None,
                                                    cached.clicks if replace_score else None)
                        receipt = 0
                        while not self._quit.is_set():
                            receipt = session.receipt(seq)
                            if receipt != 0:
                                break
                            self._quit.wait(.002)
                        if self._quit.is_set():
                            break
                        if receipt == 1:
                            installed, handled = score, goal
                        elif receipt == 2:
                            # The native slot may have swapped before revocation.
                            # Do not infer which vectors remain installed.
                            installed = handled = None
                            continue
                        else:
                            raise RuntimeError(f'native memory MIDI receipt {receipt}')
                s = session.snapshot()
                if s.closed or s.error:
                    raise RuntimeError(f'native memory MIDI worker stopped ({s.error})')
                with self._lock:
                    self._sample = Sample(goal=handled, position=s.seconds, playing=bool(s.playing),
                                          sampled_at=time.perf_counter(), error=bad_message, bad_score=bad,
                                          click_error=cached.click_error if cached and handled and cached.score is handled.score else None)
                self._wake.wait(.010)
        except Exception as exc:
            fatal = str(exc) or type(exc).__name__
        finally:
            with self._lock:
                if session is not None:
                    session.memory_fence(_CLOSE_FENCE)
                self._session = None
                self._quit.set()
            try:
                released = session.close() if session is not None else 'release not confirmed' not in (fatal or '')
            except Exception as exc:
                released = False
                fatal = f'native memory MIDI close: {exc}'
            with self._lock:
                self._retained_failed_session = None if released else session
                self._sample = Sample(error=fatal if released else (fatal or 'native MIDI output release not confirmed'),
                                      closed=True, output_released=released)
            if released:
                _LIVE.pop(id(self), None)
