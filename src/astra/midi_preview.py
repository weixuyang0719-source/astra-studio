"""Read-only MIDI piano-roll data and local Windows General MIDI playback."""

import ctypes
import json
import math
import re
import sys
import threading
from bisect import bisect_left
from collections import defaultdict, deque
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter as monotonic, perf_counter_ns  # QPC (~100 ns); time.monotonic is GetTickCount64 (15.6 ms) on this Windows

import mido
from PySide6.QtCore import QObject, QTimer, Qt, Signal

_GS_NAMES = {"microsoft gs wavetable synth", "microsoft gs 波表合成器"}
_UNAVAILABLE = "Windows MIDI \u97f3\u6e90\u4e0d\u53ef\u7528\uff08VirtualMIDISynth / Microsoft GS\uff09"

# Diagnostics sink (R13): set by the bridge only when playback tracing is on.
DIAG = None

class _MidiOutCaps(ctypes.Structure):
    _fields_ = [
        ("manufacturer", wintypes.WORD), ("product", wintypes.WORD),
        ("version", wintypes.UINT), ("name", wintypes.WCHAR * 32),
        ("technology", wintypes.WORD), ("voices", wintypes.WORD),
        ("notes", wintypes.WORD), ("channels", wintypes.WORD), ("support", wintypes.DWORD),
    ]

class _WindowsSynth:
    def __init__(self):
        self._io_lock = threading.RLock()
        self._quiescent = False
        self._release_failed = False
        self.api = None
        self.handle = ctypes.c_void_p()
        self.selected_name = ""
        if sys.platform != "win32":
            return
        try:
            self.api = ctypes.WinDLL("winmm")
        except OSError:
            return
        pointer = ctypes.c_void_p
        size = ctypes.c_size_t
        uint = wintypes.UINT
        signatures = {
            "midiOutGetNumDevs": (),
            "midiOutGetDevCapsW": (size, ctypes.POINTER(_MidiOutCaps), uint),
            "midiOutOpen": (ctypes.POINTER(pointer), uint, size, size, wintypes.DWORD),
            "midiOutShortMsg": (pointer, wintypes.DWORD),
            "midiOutReset": (pointer,),
            "midiOutClose": (pointer,),
            "midiOutGetErrorTextW": (uint, wintypes.LPWSTR, uint),
        }
        for name, arguments in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = uint

    def _caps(self, identifier):
        caps = _MidiOutCaps()
        result = self.api.midiOutGetDevCapsW(identifier, ctypes.byref(caps), ctypes.sizeof(caps))
        return None if result else caps

    @staticmethod
    def _priority(caps):
        # Only known local software synths, never MIDI mapper/hardware ports.
        if caps is None or caps.technology != 7:
            return None
        if caps.manufacturer == 65535 and re.fullmatch(r"VirtualMIDISynth #[1-9][0-9]*", caps.name):
            return (0, int(caps.name.rsplit("#", 1)[1]))
        if caps.manufacturer == 1 and caps.name.casefold() in _GS_NAMES:
            return (1, 0)
        return None

    def device(self):
        if self.api is None:
            return None
        choices = []
        for index in range(self.api.midiOutGetNumDevs()):
            caps = self._caps(index)
            priority = self._priority(caps)
            if priority is not None:
                choices.append((priority, index, caps.name))
        if not choices:
            self.selected_name = ""
            return None
        _, index, self.selected_name = min(choices)
        return index

    def _is_internal(self, identifier):
        return self._priority(self._caps(identifier)) is not None

    def _check(self, result):
        if result:
            description = ctypes.create_unicode_buffer(256)
            self.api.midiOutGetErrorTextW(result, description, len(description))
            raise OSError(description.value or f"Windows MIDI 错误 {result}")

    def open(self):
        with self._io_lock:
            if getattr(self, "_release_failed", False):
                raise OSError("Previous MIDI output release is not confirmed")
            if self.handle.value:
                return
            device = self.device()
            if device is None:
                raise OSError(_UNAVAILABLE)
            handle = ctypes.c_void_p()
            self._check(self.api.midiOutOpen(ctypes.byref(handle), device, 0, 0, 0))
            # Own the handle before verification: failed cleanup must not lose it.
            self.handle = handle
            self._release_failed = True  # No send/reopen until identity is confirmed.
            self._quiescent = False
            caps = self._caps(handle.value)
            if self._priority(caps) is None or caps.name != self.selected_name:
                self._check(self._invoke("midiOutClose", handle))
                self.handle = ctypes.c_void_p()
                self._release_failed = False
                raise OSError(_UNAVAILABLE)
            self._release_failed = False
            self._quiescent = False  # A new handle needs its own reset acknowledgement.

    def _invoke(self, name, *args):
        function = getattr(self.api, name)
        diag = DIAG
        if diag is None:
            return function(*args)
        started = perf_counter_ns()
        result = None
        try:
            result = function(*args)
            return result
        finally:
            diag.append(dict(src="winmm_call", t=monotonic(), operation=name,
                             elapsed_ns=perf_counter_ns()-started, result=result,
                             word=int(args[1]) if name=="midiOutShortMsg" else None))

    def send(self, message):
        with self._io_lock:
            if getattr(self, "_release_failed", False):
                raise OSError("Previous MIDI output release is not confirmed")
            # WinMM packs the status byte first, followed by up to two data bytes.
            word = int.from_bytes(message.bin(), "little")
            self._quiescent = False  # A failed send may still have reached the device.
            self._check(self._invoke("midiOutShortMsg", self.handle, word))

    def reset(self):
        with self._io_lock:
            if self.handle.value and not self._quiescent:
                self._check(self._invoke("midiOutReset", self.handle))
                self._quiescent = True  # Only successful reset grants this proof.

    def close(self):
        with self._io_lock:
            if self.handle.value:
                try:
                    self.reset()
                    self._check(self._invoke("midiOutClose", self.handle))
                except OSError:
                    # A closed handle is not evidence that failed reset silenced notes.
                    # Retain it for a later explicit cleanup; prevent dirty reuse.
                    self._release_failed = True
                    raise
                self.handle = ctypes.c_void_p()
                self._quiescent = False
                self._release_failed = False

@dataclass
class _Note:
    pitch: int
    start: float
    velocity: int
    channel: int
    end: float = 0.0
    sounding_end: float = 0.0
    start_beat: float = 0.0
    end_beat: float = 0.0

    def display(self):
        return {"pitch": self.pitch, "start": self.start, "end": self.end,
                "velocity": self.velocity, "channel": self.channel,
                "start_beat": self.start_beat, "end_beat": self.end_beat}

def _read_midi(path, *, include_metadata=False):
    midi = mido.MidiFile(path)
    if midi.type == 2 or midi.ticks_per_beat <= 0:
        raise ValueError("请选择采用四分拍时间基准的同步 MIDI 文件。")

    clock = 0.0
    ticks = 0
    beat = 0.0
    tempo = 500000
    tempo_map = [{"seconds": 0.0, "beat": 0.0, "tempo": tempo}]
    signatures = [{"seconds": 0.0, "beat": 0.0, "numerator": 4, "denominator": 4, "bar": 1}]
    pedal_events = []
    markers = []
    chord_positions = set()
    for track in midi.tracks:
        if track.name != 'Astra Chords':
            continue
        marker_tick = 0
        for message in track:
            marker_tick += message.time
            if message.type == 'marker':
                chord_positions.add((marker_tick, message.text))
    pedals = []
    pedal_active = {}
    events = []
    notes = []
    active = defaultdict(deque)
    sustained = defaultdict(list)
    sustain_down = [False] * 16

    def release(note):
        note.end = note.sounding_end = clock
        note.end_beat = beat
        if sustain_down[note.channel]:
            sustained[note.channel].append(note)

    def lift(channel):
        for note in sustained.pop(channel, ()):
            note.sounding_end = clock

    def pedal_change(channel, value):
        # Keep continuous CC values, including half-pedal levels below 64.
        # Playback's sustain threshold remains separate from this display data.
        previous = pedal_active.pop(channel, None)
        if previous and clock > previous["start"]:
            pedals.append({**previous, "end": clock, "end_beat": beat})
        pedal_events.append({"seconds": clock, "beat": beat, "channel": channel, "value": value})
        if value:
            pedal_active[channel] = {"channel": channel, "start": clock, "start_beat": beat, "value": value}

    for message in mido.merge_tracks(midi.tracks):
        clock += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        ticks += message.time
        beat = ticks / midi.ticks_per_beat
        if message.type == "set_tempo":
            if message.tempo <= 0:
                raise ValueError("MIDI 文件包含无效的速度。")
            tempo = message.tempo
            point = {"seconds": clock, "beat": beat, "tempo": tempo}
            if tempo_map[-1]["beat"] == beat:
                tempo_map[-1] = point
            elif tempo_map[-1]["tempo"] != tempo:
                tempo_map.append(point)
        elif message.type == "time_signature":
            previous = signatures[-1]
            bar_length = previous["numerator"] * 4 / previous["denominator"]
            elapsed_bars = (beat - previous["beat"]) / bar_length
            if ((previous["numerator"], previous["denominator"]) == (message.numerator, message.denominator)
                    and abs(elapsed_bars-round(elapsed_bars)) < 1e-9):
                continue
            # A repeated signature at an off-bar position is an explicit phase
            # reset (e.g. the first complete bar after a short intro). Dropping
            # it makes the exported-MIDI view disagree with the working score.
            # A signature change ends the previous measure. Well-formed exports
            # put it exactly on a bar line; imported mid-bar changes start a new bar.
            bar = previous["bar"] + math.ceil((beat - previous["beat"]) / bar_length - 1e-9)
            signature = {"seconds": clock, "beat": beat, "numerator": message.numerator,
                         "denominator": message.denominator, "bar": bar}
            if previous["beat"] == beat:
                signatures[-1] = signature
            else:
                signatures.append(signature)
        if message.type == 'marker' and (ticks, message.text) in chord_positions:
            markers.append({'seconds':clock,'beat':beat,'label':message.text})
        if not hasattr(message, "channel"):
            continue
        events.append((clock, message))
        channel = message.channel
        if message.type == "note_on" and message.velocity:
            note = _Note(message.note, clock, message.velocity, channel, start_beat=beat)
            active[channel, message.note].append(note)
            notes.append(note)
        elif message.type in {"note_on", "note_off"}:
            queue = active.get((channel, message.note))
            if queue:
                release(queue.popleft())
        elif message.type == "control_change":
            if message.control == 64:
                pedal_change(channel, message.value)
                sustain_down[channel] = message.value >= 64
                if not sustain_down[channel]:
                    lift(channel)
            elif message.control == 121:
                if channel in pedal_active:
                    pedal_change(channel, 0)
                sustain_down[channel] = False
                lift(channel)
            elif message.control == 120 or message.control >= 123:
                for (owner, _), queue in active.items():
                    if owner == channel:
                        while queue:
                            release(queue.popleft())
                if message.control == 120:
                    lift(channel)

    for queue in active.values():
        for note in queue:
            note.end = note.sounding_end = clock
            note.end_beat = beat
    for channel in list(sustained):
        lift(channel)
    for previous in pedal_active.values():
        if clock > previous["start"]:
            pedals.append({**previous, "end": clock, "end_beat": beat})
    if include_metadata:
        return events, notes, clock, {"tempo_map": tempo_map, "time_signatures": signatures,
                                     "pedals": pedals, "pedal_events": pedal_events,
                                     "duration_beats": beat, "markers": markers}
    return events, notes, clock

# Controller values a synth returns to on "Reset All Controllers" (MIDI RP-015 / GM2 defaults).
# Restore uses them for controllers the synth has seen but the target position does not set,
# instead of a blanket CC121 (whose voice-level side effects caused the R15 pops). Controllers not
# listed (e.g. data entry 6/38, which depends on the selected RPN) are not guessed.
_CC_DEFAULTS = {1: 0, 2: 0, 4: 0, 5: 0, 8: 64, 10: 64, 11: 127, 12: 0, 13: 0, 16: 0, 17: 0, 18: 0, 19: 0,
                **{c: 0 for c in range(33, 64)}, 0: 0, 32: 0, 64: 0, 65: 0, 66: 0, 67: 0, 68: 0, 69: 0,
                **{c: 64 for c in range(70, 80)}, 80: 0, 81: 0, 82: 0, 83: 0, 84: 0, 91: 40, 92: 0, 93: 0,
                94: 0, 95: 0, 98: 127, 99: 127, 100: 127, 101: 127}
_HOLD_PEDALS = (64, 66, 69)  # sustain, sostenuto, hold 2: each can keep a released note sounding

class _MidiStateTracker:
    """What the synth has actually been told (Ardour MidiNoteTracker + MidiStateTracker):
    per (channel, note) on-counts, so overlapping same-pitch notes keep a correct lifetime; and the
    last controller / pitch-bend / pressure / program value per channel."""

    def __init__(self):
        self.notes = defaultdict(int)
        self.controls = {}
        self.pitch = {}
        self.pressure = {}
        self.poly = {}
        self.program = {}

    def track(self, message):
        kind = message.type
        if kind == "note_on" and message.velocity > 0:
            self.notes[message.channel, message.note] += 1
        elif kind in {"note_on", "note_off"}:
            key = (message.channel, message.note)
            if self.notes.get(key, 0) > 1:
                self.notes[key] -= 1
            else:
                self.notes.pop(key, None)
        elif kind == "control_change":
            if message.control in (120, 123):
                for key in [k for k in self.notes if k[0] == message.channel]:
                    del self.notes[key]
            elif message.control == 121:
                for key in [k for k in self.controls if k[0] == message.channel and k[1] in _CC_DEFAULTS and k[1] not in (0, 7, 10, 32, 91, 93)]:
                    del self.controls[key]
                self.pitch.pop(message.channel, None); self.pressure.pop(message.channel, None)
                for key in [k for k in self.poly if k[0] == message.channel]:
                    del self.poly[key]
            else:
                self.controls[message.channel, message.control] = message.value
        elif kind == "pitchwheel":
            self.pitch[message.channel] = message.pitch
        elif kind == "aftertouch":
            self.pressure[message.channel] = message.value
        elif kind == "polytouch":
            self.poly[message.channel, message.note] = message.value
        elif kind == "program_change":
            self.program[message.channel] = message.program

    def channels(self):
        return ({c for c, _ in self.notes} | {c for c, _ in self.controls} | set(self.pitch)
                | set(self.pressure) | {c for c, _ in self.poly} | set(self.program))

def _emit(owner, message):
    """Send to the synth and record it (R15b tracker). Objects without a tracker only send."""
    owner._synth.send(message)
    tracker = getattr(owner, '_tracker', None)
    if tracker is not None:
        tracker.track(message)

def prepared_midi_view(path, prepared):
    """Project parsed data for the UI without owning a synth or a note timer."""
    _, notes, duration, metadata = prepared
    display_metadata = dict(metadata)
    display_metadata.pop('_synth_available', None)
    return {"notes": [note.display() for note in notes], "duration": duration,
            "name": Path(path).stem, "path": str(path), **display_metadata}

class MidiPreview(QObject):
    """A lightweight monotonic MIDI transport bound to the Qt GUI thread."""

    changed = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None, *, available_hint=None):
        super().__init__(parent)
        self._synth = _WindowsSynth()
        self._available = available_hint if type(available_hint) is bool else self._synth.device() is not None
        self._message = (getattr(self._synth, "selected_name", "") or "Windows MIDI") if self._available else _UNAVAILABLE
        self._tracker = _MidiStateTracker()  # what the synth has actually been told
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._events = []
        self._event_times = []
        self._notes = []
        self._channels = []
        self._index = 0
        self._duration = 0.0
        self._position = 0.0
        self._started = None
        self._last_update = 0.0
        self._volume = 100
        self._rate = 1.0
        self._loop = None
        self._performance_events = []
        self._metronome_signature = None
        self._channel_volumes = [100] * 16

    @property
    def duration(self):
        return self._duration

    @property
    def position(self):
        if self._started is None:
            return self._position
        return min(self.duration, self._position + (monotonic() - self._started)*self._rate)

    @property
    def playing(self):
        return self._started is not None

    @property
    def available(self):
        return self._available

    @property
    def message(self):
        return self._message

    def state(self):
        return {"position": self.position, "duration": self.duration, "playing": self.playing,
                "rate":self._rate,
                "available": self.available, "message": self.message}

    def _publish(self):
        self._last_update = monotonic()
        self.changed.emit(json.dumps(self.state(), ensure_ascii=False))

    def load(self, path):
        path = Path(path).expanduser().resolve()
        try:
            prepared = _read_midi(path, include_metadata=True)
        except (OSError, EOFError, ValueError) as error:
            raise ValueError(f"无法读取 MIDI 文件。{error}") from error
        return self.load_prepared(path, prepared)

    def load_prepared(self, path, prepared):
        """Install worker-parsed data; synth operations stay on the GUI thread."""
        events, notes, duration, metadata = prepared
        self._halt(0.0)
        # Empty channels often carry default programs and duplicated pedals.
        # Restoring them on every seek costs native synth calls but cannot sound.
        self._channels = sorted({note.channel for note in notes})
        self._events = [(at, message) for at, message in events if message.channel in self._channels]
        self._performance_events = list(self._events)
        # The click cache belongs to this performance, including its duration.
        # The same tempo map can be reused when switching MIDI variants.
        self._metronome_signature = None
        self._event_times = [timestamp for timestamp, _ in self._events]
        self._notes = notes
        self._index = 0
        self._duration = duration
        hint = metadata.get('_synth_available')
        self._available = hint if type(hint) is bool else self._synth.device() is not None
        self._message = (getattr(self._synth, "selected_name", "") or "Windows MIDI") if self.available else _UNAVAILABLE
        self._publish()
        # Display all authored controller data, including on silent channels.
        return prepared_midi_view(path, prepared)

    def _send_volume(self, channel):
        level = round(self._channel_volumes[channel] * self._volume / 100)
        _emit(self, mido.Message("control_change", channel=channel, control=7, value=level))

    @property
    def _active(self):
        """(channel, note) keys with at least one voice keyed on (read-only view)."""
        return set(self._tracker.notes)

    def _out(self, message):
        _emit(self, message)

    def _resolve(self):
        """Release what is sounding (Ardour MidiTrack::realtime_handle_transport_stopped): one
        note-off per tracked voice (overlapping same-pitch notes included), then every hold pedal
        (sustain, sostenuto, hold 2) up and All Notes Off per channel. Voices end through their
        release envelope; no midiOutReset / All Sound Off hard cut."""
        voices = sorted(self._tracker.notes.items())
        channels = sorted({c for (c, _), _n in voices} | set(self._channels) | self._tracker.channels())
        for (channel, note), count in voices:
            for _ in range(count):
                _emit(self, mido.Message("note_off", channel=channel, note=note))
        for channel in channels:
            for pedal in _HOLD_PEDALS:
                if pedal == 64 or self._tracker.controls.get((channel, pedal), 0) or (channel, pedal) in self._tracker.controls:
                    _emit(self, mido.Message("control_change", channel=channel, control=pedal, value=0))
            _emit(self, mido.Message("control_change", channel=channel, control=123, value=0))

    def _reset_strays(self, target):
        """Return what the synth still holds but the target state does not set to its default:
        controllers (RP-015 table), pitch bend, channel/poly pressure, on every channel the synth
        has seen (incl. a previous file's). Precise, instead of a blanket CC121."""
        tracker = self._tracker
        for (channel, control), value in sorted(tracker.controls.items()):
            if (channel, control) in target or control in (7,):
                continue
            default = _CC_DEFAULTS.get(control)
            if default is not None and value != default:
                _emit(self, mido.Message("control_change", channel=channel, control=control, value=default))
        for channel, value in sorted(tracker.pitch.items()):
            if value and (channel, "pitch") not in target:
                _emit(self, mido.Message("pitchwheel", channel=channel, pitch=0))
        for channel, value in sorted(tracker.pressure.items()):
            if value and (channel, "pressure") not in target:
                _emit(self, mido.Message("aftertouch", channel=channel, value=0))
        for (channel, note), value in sorted(tracker.poly.items()):
            if value:
                _emit(self, mido.Message("polytouch", channel=channel, note=note, value=0))

    def _send(self, message):
        if message.type == "control_change" and message.control == 7:
            self._channel_volumes[message.channel] = message.value
            self._send_volume(message.channel)
        else:
            _emit(self, message)
            if message.type == "control_change" and message.control == 121:
                self._send_volume(message.channel)

    def _restore(self, position):
        # No device reset and no All Sound Off / Reset All Controllers here: those cut every voice
        # instantly (audible pop on start and seek). _halt already released sounding notes; the
        # explicit baseline below sets the controllers that matter, then the chase restores state.
        self._channel_volumes = [100] * 16
        # Target state at `position`: the baseline for this file's channels plus the chased events.
        target = {(c, k) for c in self._channels for k in (64, 66, 0, 32, 10, 11, 91, 93)}
        target |= {(c, "pitch") for c in self._channels}
        for _at, message in self._events[:bisect_left(self._event_times, position)]:
            if message.type == "control_change":
                target.add((message.channel, message.control))
            elif message.type == "pitchwheel":
                target.add((message.channel, "pitch"))
            elif message.type == "aftertouch":
                target.add((message.channel, "pressure"))
        reset = getattr(self, '_reset_strays', None)  # minimal stand-ins (tests) have no tracker
        if reset is not None and hasattr(self, '_tracker'):
            reset(target)
        for channel in self._channels:
            # Establish a known baseline after pause, seek or file switching.
            for control, value in ((64, 0), (66, 0), (0, 0), (32, 0),
                                   (10, 64), (11, 127), (91, 40), (93, 0)):
                _emit(self, mido.Message("control_change", channel=channel, control=control, value=value))
            _emit(self, mido.Message("program_change", channel=channel, program=0))
            _emit(self, mido.Message("pitchwheel", channel=channel, pitch=0))
            self._send_volume(channel)
        self._index = bisect_left(self._event_times, position)
        if not self._index:
            return

        # Restore last controller/bend values. Bank, program, and parameter
        # operations retain order because they affect subsequent messages.
        state = {}
        ordered = []
        for index in range(self._index):
            message = self._events[index][1]
            if message.type in {"note_on", "note_off"}:
                continue
            if message.type == "program_change" or (
                message.type == "control_change" and
                (message.control in {0, 6, 32, 38, 96, 97, 98, 99, 100, 101} or message.control >= 120)
            ):
                ordered.append((index, message))
            else:
                discriminator = getattr(message, "control", getattr(message, "note", None))
                state[message.channel, message.type, discriminator] = (index, message)
        for _, message in sorted((*ordered, *state.values())):
            self._send(message)

        for note in self._notes:
            if note.start < position < note.sounding_end:
                _emit(self, mido.Message("note_on", channel=note.channel, note=note.pitch, velocity=note.velocity))
                if note.end < position:
                    _emit(self, mido.Message("note_off", channel=note.channel, note=note.pitch))

    def _halt(self, position):
        was_playing = self._started is not None
        self._timer.stop()
        self._started = None
        self._position = position
        try:
            if was_playing or self._tracker.notes:
                self._resolve()
        except OSError as error:
            self._fail(error)

    def _fail(self, error):
        self._position = self.position
        self._started = None
        self._timer.stop()
        released = False
        try:
            self._synth.close()
            released = True
        except OSError:
            pass
        if released:
            self._tracker = _MidiStateTracker()
        self._available = released and self._synth.device() is not None
        self._message = f"MIDI 试听失败。{error}"
        self.error.emit(self._message)
        self._publish()

    def play(self):
        if self.playing or not self._events:
            return
        if self._position >= self.duration:
            self._position = 0.0
        # Inverted locators skip [R,L); starting within the gap must chase L
        # before any excluded notes or controllers are emitted.
        if self._loop and self._loop[1]<=self._position<self._loop[0]:
            self._position=self._loop[0]
        try:
            self._synth.open()
            self._restore(self._position)
            self._message = getattr(self._synth, "selected_name", "") or "Windows MIDI"
            self._available = True
            self._started = monotonic()
            self._timer.start()
            self._publish()
            self._tick()
        except OSError as error:
            self._fail(error)

    def pause(self):
        if self.playing:
            self._halt(self.position)
            self._publish()

    def stop(self):
        self._halt(0.0)
        self._publish()

    def seek(self, seconds):
        seconds = float(seconds)
        if not math.isfinite(seconds):
            return
        resume = self.playing
        self._halt(max(0.0, min(self.duration, seconds)))
        if resume and self._position < self.duration:
            self.play()
        else:
            self._publish()

    def set_rate(self, rate):
        rate=float(rate)
        if not math.isfinite(rate) or not .25<=rate<=2:
            raise ValueError('试听速度应为 0.25–2 倍。')
        position=self.position
        self._position=position
        if self.playing:self._started=monotonic()
        self._rate=rate

    def set_loop(self, start, end, enabled=True):
        start,end=float(start),float(end)
        self._loop=(start,end) if (enabled and math.isfinite(start) and math.isfinite(end)
                and 0<=min(start,end) and max(start,end)<=self.duration+.001 and start!=end) else None

    def set_metronome(self, tempo_data, enabled):
        from .timing import TempoMap
        signature=(str(tempo_data),bool(enabled))
        if getattr(self,'_metronome_signature',None)==signature:return
        self._metronome_signature=signature
        playing=self.playing;position=self.position
        if playing:self.pause()
        events=list(self._performance_events)
        self._channels=sorted({note.channel for note in self._notes}|({9} if enabled else set()))
        if enabled:
            clock=TempoMap(tempo_data);meters=tempo_data['time_signatures']
            for i,meter in enumerate(meters):
                start=meter['beat'];stop=meters[i+1]['beat'] if i+1<len(meters) else clock.to_beat(self.duration)
                unit=4/meter['denominator'];index=0
                if i==0:start=float(tempo_data.get('grid_origin',tempo_data.get('pickup_beats',0)))
                beat=start
                while beat<stop-1e-8:
                    at=clock.to_seconds(beat);pitch=76 if index%meter['numerator']==0 else 77
                    events.extend([(at,mido.Message('note_on',channel=9,note=pitch,velocity=95 if index%meter['numerator']==0 else 60)),
                                   (at+.045,mido.Message('note_off',channel=9,note=pitch,velocity=0))])
                    beat+=unit;index+=1
        self._events=sorted(events,key=lambda item:item[0])
        self._event_times=[at for at,_ in self._events]
        self._index=bisect_left(self._event_times,position)
        self._position=position
        if playing:self.play()

    def set_volume(self, level):
        level = float(level)
        if not math.isfinite(level):
            return
        self._volume = round(max(0, min(100, level)))
        if self.playing:
            try:
                for channel in self._channels:
                    self._send_volume(channel)
            except OSError as error:
                self._fail(error)

    def _tick(self):
        if not self.playing:
            return
        position = self.position
        diag = DIAG
        if diag is not None and self._index < len(self._event_times) and self._event_times[self._index] <= position:
            late = position - self._event_times[self._index]
            if late > .02:  # first due event sent more than 20 ms after its time
                diag.append(dict(src='midi_late', t=round(monotonic(), 6), late_ms=round(late * 1000, 1)))
        if self._loop and ((self._loop[0]<self._loop[1] and position>=self._loop[1])
                or (self._loop[1]<=position<self._loop[0])):
            # Restore pre-boundary controls and sounding notes on every iteration.
            self.seek(self._loop[0])
            return
        try:
            # Bound each timer turn when a long GUI stall makes events overdue.
            limit = min(len(self._events), self._index + 256)
            while self._index < limit and self._event_times[self._index] <= position:
                self._send(self._events[self._index][1])
                self._index += 1
        except OSError as error:
            self._fail(error)
            return
        if position >= self.duration and self._index == len(self._events):
            self._halt(self.duration)
            self._publish()
        elif monotonic() - self._last_update >= 1 / 30:
            self._publish()

    def close(self):
        self._halt(0.0)
        try:
            self._synth.close()
        except OSError as error:
            self._fail(error)
        self._events = []
        self._performance_events = []
        self._metronome_signature = None
        self._loop = None
        self._event_times = []
        self._notes = []
        self._channels = []
        self._index = 0
        self._duration = 0.0
        self._publish()
