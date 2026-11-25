"""Frame-domain click for the legacy native owner. This module opens no output."""
from dataclasses import dataclass
import math
import threading

import numpy as np

from astra.shared_audio_core import RATE, project_frames
from .native_metronome import metronome_beats

MAX_BEATS = 100_000


@dataclass(frozen=True)
class PcmClickStatus:
    requested: bool = False
    prepared: bool = False  # A usable grid, not an assertion that a click is audible.
    preparing: bool = False
    error: str | None = None
    beat_count: int = 0
    click_gain: float = 1.
    master_gain: float = 1.
    # Since the latest applied request, on this owner. Submission is not consumption.
    submitted_onsets: int = 0
    last_onset_frame: int | None = None  # Project frame, not device-buffer frame.
    submitted_peak: float = 0.  # Largest absolute post-clip change to either dry channel.


@dataclass(frozen=True)
class _Plan:
    starts: np.ndarray
    ends: np.ndarray
    accents: np.ndarray
    waves: np.ndarray


def _waveforms():
    # Short, softly attacked woodblock-like pulses. Row 1 is the downbeat.
    t = np.arange(round(.024 * RATE), dtype=np.float64) / RATE
    env = np.minimum(t / .0015, 1.) * np.exp(-t / .006)
    env *= np.minimum((t[-1] - t) / .003, 1.)
    return np.stack([
        level * env * (.8 * np.sin(2 * np.pi * hz * t) + .2 * np.sin(2 * np.pi * 1.7 * hz * t))
        for hz, level in ((850, .20), (1300, .32))
    ]).astype(np.float32)


def _prepare(tempo, duration, cancel):
    if cancel.is_set():
        return None
    if not isinstance(tempo, dict):
        raise ValueError('No tempo map for audio metronome')
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid audio duration for metronome')
    end = round(duration * RATE)
    if not 0 < end < np.iinfo(np.int64).max // 2:
        raise ValueError('Audio duration is outside the metronome frame range')
    starts, accents = [], []
    for count, (seconds, accent) in enumerate(metronome_beats(tempo, duration), 1):
        if cancel.is_set():
            return None
        if count > MAX_BEATS:
            raise ValueError('Metronome event budget exceeded')
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Invalid metronome beat time')
        frame = round(seconds * RATE)
        if frame >= end:
            continue
        if starts and frame < starts[-1]:
            raise ValueError('Metronome beats are out of order')
        if starts and frame == starts[-1]:
            accents[-1] |= int(bool(accent))  # One pulse per frame; the accent wins.
        else:
            starts.append(frame)
            accents.append(int(bool(accent)))
    if not starts:
        raise ValueError('No metronome beats within the audio duration')
    starts = np.asarray(starts, dtype=np.int64)
    accents = np.asarray(accents, dtype=np.intp)
    waves = _waveforms()
    # Non-overlapping intervals make lookup bounded even at very dense grids.
    ends = np.minimum(starts + waves.shape[1], np.append(starts[1:], np.int64(end)))
    for a in (starts, ends, accents, waves):
        a.setflags(write=False)
    return _Plan(starts, ends, accents, waves)


class PcmMetronome:
    """Owner-thread gate and mixer; a one-slot mailbox to a preparation worker."""

    def __init__(self, duration):
        self._duration = duration
        self._requested = False
        self._gain = 1.
        self._plan = self._error = None
        self._next_frame = None
        self._origin = 0
        self._unsubmitted = None
        self._submitted_onsets, self._last_onset_frame, self._submitted_peak = 0, None, 0.
        self._cv = threading.Condition()
        self._worker = self._ticket = self._job = self._result = None
        self._closed = False

    def _discard_locked(self):
        if self._ticket is not None:
            self._ticket.set()
        self._ticket = self._job = self._result = None

    def request(self, tempo, enabled):
        # This runs in transport command order, before any further normal write.
        self._requested = enabled
        self._plan = self._error = None
        self._submitted_onsets, self._last_onset_frame, self._submitted_peak = 0, None, 0.
        self.discontinuity()
        with self._cv:
            self._discard_locked()
            if not enabled:
                return
            self._ticket = threading.Event()
            self._job = (self._ticket, tempo)
            if self._worker is None:
                try:
                    self._worker = threading.Thread(target=self._work, name='Astra-RAW-click-grid', daemon=True)
                    self._worker.start()
                except Exception as exc:
                    self._discard_locked()
                    self._worker = None
                    self._error = 'Click preparation could not start: ' + repr(exc)
                    return
            self._cv.notify()

    def _work(self):
        cached = None  # Last successful, immutable grid; off/on need not expand it again.
        while True:
            with self._cv:
                while not self._closed and self._job is None:
                    self._cv.wait()
                if self._closed:
                    return
                ticket, tempo = self._job
                self._job = None
            plan = error = None
            try:
                plan = cached[1] if cached is not None and tempo == cached[0] else _prepare(tempo, self._duration, ticket)
            except Exception as exc:
                error = 'Click preparation failed: ' + repr(exc)[:200]
            with self._cv:
                if not self._closed and ticket is self._ticket and not ticket.is_set():
                    if plan is not None:
                        cached = (tempo, plan)
                    self._result = (plan, error)

    def poll(self):
        if self._ticket is None:
            return
        with self._cv:
            if self._result is None:
                return
            self._plan, self._error = self._result
            self._ticket = self._result = None

    def close(self):
        # No device/cache references on the worker and no join in the release fence.
        with self._cv:
            self._closed = True
            self._discard_locked()
            self._cv.notify()

    def status(self, master_gain=1.):
        return PcmClickStatus(self._requested, self._plan is not None,
                              self._ticket is not None, self._error,
                              int(self._plan.starts.size) if self._plan is not None else 0,
                              self._gain, master_gain, self._submitted_onsets,
                              self._last_onset_frame, self._submitted_peak)

    def submitted(self):
        # The owner calls this only after the regular device.write succeeded.
        mark, self._unsubmitted = self._unsubmitted, None
        if mark is not None:
            onsets, last, peak = mark
            self._submitted_onsets += onsets
            if last is not None:
                self._last_onset_frame = last
            self._submitted_peak = max(self._submitted_peak, peak)

    def discontinuity(self):
        self._next_frame = None
        self._unsubmitted = None

    def set_gain(self, gain):
        self._gain = gain
        if gain == 0:
            self.discontinuity()

    def render(self, timeline, count, gain):
        self._unsubmitted = None
        if self._plan is None:
            return (timeline.render(count) * gain).astype('float32', copy=False)
        frame, loop = timeline.cursor, timeline.loop
        # Render music first (including the existing source/jump fades). The
        # scaled result owns its storage; the cache never sees a mixed buffer.
        out = (timeline.render(count) * gain).astype('float32', copy=False)
        return self.mix(out, frame, loop, gain)

    def _hits(self, plan, frames, tail_only):
        breaks = np.flatnonzero(np.diff(frames) != 1) + 1
        first = int(frames[0])
        if tail_only:
            if first != self._next_frame:
                return None
            i = int(np.searchsorted(plan.starts, first - 1, side='right')) - 1
            if i < 0 or plan.starts[i] < self._origin:
                return None
            count = min(int(breaks[0]) if breaks.size else len(frames), int(plan.ends[i]) - first)
            if count <= 0:
                return None
            return np.arange(count), plan.waves[plan.accents[i], frames[:count] - plan.starts[i]], 0, None

        # Carry a tail only across contiguous submissions. At a wrap, skip,
        # seek, or newly opened gate require the onset to be in the new run.
        origin = self._origin if first == self._next_frame else first
        if breaks.size:
            floor = np.repeat(np.r_[origin, frames[breaks]], np.diff(np.r_[0, breaks, len(frames)]))
            self._origin = int(frames[breaks[-1]])
        else:
            floor = self._origin = origin
        self._next_frame = int(frames[-1]) + 1
        i = np.searchsorted(plan.starts, frames, side='right') - 1
        j = np.maximum(i, 0)
        at = np.flatnonzero((i >= 0) & (plan.starts[j] >= floor) & (frames < plan.ends[j]))
        hits = j[at]
        offsets = frames[at] - plan.starts[hits]
        onsets = np.flatnonzero(offsets == 0)
        last = int(frames[at[onsets[-1]]]) if onsets.size else None
        return at, plan.waves[plan.accents[hits], offsets], len(onsets), last

    def mix(self, out, frame, loop, gain, *, fade=None):
        if self._plan is None or not self._gain or not gain or not len(out):
            self.discontinuity()
            return out
        try:
            # This is bounded by the submission, even for a one-frame loop.
            # No tempo calculations, PCM cache reads, or per-lap Python loop.
            hits = self._hits(self._plan, project_frames(frame, len(out), loop), fade is not None)
            if hits is not None:
                at, values, onsets, last = hits
                values = values * (self._gain * gain)
                if fade is not None:
                    values *= fade[at, 0]
                nonzero = values != 0
                at = at[nonzero]
                peak = 0.
                if at.size:
                    # Limit only samples receiving a click. Keep the existing
                    # gain law and add the same pulse to both stereo channels.
                    dry = out[at]
                    wet = np.clip(dry + values[nonzero, None], -1., 1.)
                    out[at] = wet
                    if fade is None:
                        peak = float(np.max(np.abs(wet - dry)))
                        if not math.isfinite(peak):
                            peak = 0.  # Never put a non-finite number in the JSON snapshot.
                if fade is None and (onsets or peak):
                    # An onset can have a zero attack sample; the peak is separate
                    # evidence of a PCM change. Pause tails do not count as new beats.
                    self._unsubmitted = (onsets, last, peak)
        except Exception as exc:
            self._plan = None
            self._error = 'Click mixing failed: ' + repr(exc)[:200]
            self.discontinuity()
        return out
