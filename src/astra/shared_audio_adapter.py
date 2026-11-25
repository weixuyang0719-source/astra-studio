"""Guarded shared-output adapter. Established Qt media player remains fallback."""
import time, math, os, threading
from PySide6.QtCore import QIODevice, QTimer
from PySide6.QtMultimedia import QAudioSink, QAudioFormat, QMediaDevices
from .shared_audio_core import ChunkCache, Timeline, RATE, loop_position, loop_repeats

class Stream(QIODevice):

    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline
        self.failure = None
        self._read_lock = threading.RLock()
        self._remainder = b''
        self.unbuffered = os.environ.get('ASTRA_UNBUFFERED_AUDIO', '0') == '1'
        mode = QIODevice.OpenModeFlag.ReadOnly
        if self.unbuffered:
            mode |= QIODevice.OpenModeFlag.Unbuffered
        self.open(mode)

    def isSequential(self):
        return True

    def bytesAvailable(self):
        return 65536 + super().bytesAvailable()

    def readData(self, n):
        if n <= 0:
            return b''
        try:
            with self._read_lock:
                missing = max(0, n - len(self._remainder))
                block = self.timeline.render((missing + 7) // 8).tobytes() if missing else b''
                data = self._remainder + block
                self._remainder = data[n:]
                return data[:n]
        except Exception as e:
            self.failure = repr(e)
            return b''

    def reset_read_state(self):
        # Caller must stop/reset the sink before taking the source read lock.
        with self._read_lock:
            self._remainder = b''
            self.failure = None
            # Also discard QIODevice's own read-ahead in the compatibility path.
            mode = self.openMode()
            self.close()
            self.open(mode)

    def writeData(self, data):
        return -1

class SharedAudioAdapter:
    timeline_type = Timeline

    def __init__(self, bridge, enabled=False):
        self.bridge = bridge
        self.active = False
        self.reason = 'disabled'
        self.playing = False
        self.started = False
        self.position_hold = 0.0
        self.loop = None
        self.output_loop = None
        self.last_error = None
        self.timer = QTimer(bridge)
        self.timer.timeout.connect(self._tick)
        if not enabled:
            return
        sources = bridge._sources
        try:
            if not sources:
                raise ValueError('No sources')
            if len(sources) > 3:
                raise ValueError('More than three sources uses legacy')
            if getattr(bridge, '_audition_together', False):
                raise ValueError('Joint MIDI audition uses legacy')
            if getattr(bridge, '_audition_rate', 1) != 1:
                raise ValueError('Non-1x rate uses legacy')
            self.keys = [s['key'] for s in sources]
            self.cache = ChunkCache([s['path'] for s in sources], limit=max(24, len(sources) * 10))
            if any(info.format not in {'WAV', 'WAVEX', 'FLAC'} for info in self.cache.info):
                raise ValueError('Unverified codec uses legacy playback')
            self.timeline = self.timeline_type(self.cache)
            self.stream = Stream(self.timeline)
            durations = [i.duration for i in self.cache.info]
            if max(durations) - min(durations) > 0.05:
                raise ValueError('Unaligned source durations use legacy')
            self.duration = max(durations)
            fmt = QAudioFormat()
            fmt.setSampleRate(RATE)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            device = QMediaDevices.defaultAudioOutput()
            if not device.isFormatSupported(fmt):
                raise ValueError('Float32 48k stereo unsupported')
            self.sink = QAudioSink(device, fmt)
            self.sink.setBufferSize(RATE * 8 // 50)
            self.sink.setVolume(0 if bridge._muted else bridge._volume / 100)
        except Exception as e:
            if hasattr(self, 'cache'):
                self.cache.close()
            self.reason = str(e)
            return
        selection = getattr(bridge, '_audition_range', None) or ((getattr(bridge, '_project', None) or {}).get('editor') or {}).get('range') or {}
        self.range_state = {'start': selection.get('start', 0), 'end': selection.get('end', self.duration), 'loop': selection.get('loop', False)}
        try:
            self._validate_range(**self.range_state)
        except (TypeError, ValueError):
            self.cache.close()
            self.reason = 'Invalid loop settings use legacy'
            return
        state = bridge._player_state(False)
        self.source = state['source']
        self.position_hold = state['position']
        self.playing = state['playing']
        self.timeline.source = self.keys.index(self.source)
        self.timeline.old = self.timeline.source
        self.timeline.seek(round(self.position_hold * RATE))
        self.base = self.position_hold
        self.cache.prefetch(self.timeline.cursor)
        self.pending_since = time.monotonic()
        # Retire any pending legacy async resume before taking audio ownership.
        bridge._pending_audio = None
        if state['playing']:
            bridge._media.pause()
        self.active = True
        self.reason = 'shared 1x'
        self.timer.start(20)

    def position(self):
        if not self.active:
            return self.bridge._player_state(False)['position']
        value = self.base + self.sink.processedUSecs() / 1000000.0 if self.started else self.position_hold
        loop = self.output_loop if self.started else None
        if loop:
            value = loop_position(value, loop, self.base)
        return value

    def _tick(self):
        if not self.active:
            return
        if self.sink.error().value != 0:
            self.fallback('audio device error')
            return
        if self.playing and not self.started and self.loop:
            target = loop_position(self.position_hold, self.loop)
            if target != self.position_hold:
                self.seek(target)
        if self.playing and (not loop_repeats(self.loop)) and (self.position() >= self.duration) and not (getattr(self, 'pending_jump', None) or getattr(self, '_awaiting_jump_audio', False)):
            self.pause()
            self.seek(self.duration)
            return
        self.cache.prefetch(self.timeline.cursor)
        if self.loop:
            self.cache.prefetch(round(self.loop[0] * RATE))
        if self.stream.failure or self.cache.failures:
            self.fallback(self.stream.failure or str(self.cache.failures))
            return
        if self.playing and (not self.started):
            if not self._startup_ready():
                if time.monotonic() - self.pending_since > 5:
                    self.fallback('decode timeout')
                return
            self.base = self.position_hold
            self.output_loop = self.loop
            self.sink.start(self.stream)
            self.started = True

    def _startup_ready(self):
        # Preserve the entire existing prefetch window (all sources, loop wrap
        # and crossfade included), but never wait for obsolete drag destinations.
        frames = [self.timeline.cursor]
        if self.loop:
            frames.append(round(self.loop[0] * RATE))
        required = {(source, second + offset)
                    for frame in frames for source in range(len(self.keys))
                    for second in [max(0, int(frame) // RATE)]
                    for offset in range(-1, 4) if second + offset >= 0}
        with self.cache.lock:
            return required.issubset(self.cache.data)

    def select(self, key):
        if not self.active:
            self.bridge._select_audio(key, True)
            return
        if key not in self.keys:
            self.fallback('unknown source')
            self.bridge._select_audio(key, True)
            return
        self.timeline.select(self.keys.index(key))
        self.source = key

    def play(self):
        if not self.active:
            if not self.bridge._player_state(False)['playing']:
                self.bridge._play_pause()
            return
        if not loop_repeats(self.loop) and self.position() >= self.duration:
            self.seek(0)
        self.playing = True
        if self.started:
            self.sink.resume()
        else:
            self.pending_since = time.monotonic()
            # Ready PCM should not wait for the next 20ms housekeeping tick.
            # Cold destinations remain asynchronous through the same readiness
            # gate; no warm/silent device is started while paused.
            self._tick()

    def pause(self):
        if not self.active:
            if self.bridge._player_state(False)['playing']:
                self.bridge._play_pause()
            return
        self.sink.suspend()
        self.playing = False

    def seek(self, seconds):
        if not self.active:
            self.bridge._seek_audio(seconds)
            return
        seconds = float(seconds)
        if not math.isfinite(seconds):
            raise ValueError('invalid position')
        self.sink.reset()
        self.stream.reset_read_state()
        self.started = False
        self.position_hold = min(self.duration, max(0, seconds))
        self.base = self.position_hold
        self.timeline.seek(round(self.position_hold * RATE))
        self.pending_since = time.monotonic()

    def stop(self):
        self.pause()
        self.seek(0)

    @staticmethod
    def _validate_range(start, end, loop):
        if type(loop) is not bool or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)) or start < 0 or end < 0:
            raise ValueError('invalid loop settings')

    def set_loop(self, start, end, enabled):
        self._validate_range(start, end, enabled)
        if not self.active:
            self.bridge._audition_range = {'start': start, 'end': end, 'loop': enabled}
            return
        self.range_state = {'start': start, 'end': end, 'loop': enabled}
        if start < 0 or end < 0 or max(start, end) > self.duration:
            self.fallback('loop outside source duration')
            self.bridge._audition_range = {'start': start, 'end': end, 'loop': enabled}
            return
        previous_position = self.position()
        frame_loop = (round(start * RATE), round(end * RATE))
        self.loop = (start, end) if enabled and frame_loop[0] != frame_loop[1] else None
        self.timeline.loop = frame_loop if self.loop else None
        if self.started:
            self.seek(previous_position)
        if enabled:
            self.cache.prefetch(round(start * RATE))
            if self.playing:
                target = loop_position(previous_position, self.loop)
                if target != previous_position:
                    self.seek(target)

    def set_rate(self, rate):
        if not 0.25 <= rate <= 2:
            raise ValueError('invalid rate')
        if self.active and rate != 1:
            self.fallback('non-1x rate')
        if not self.active:
            self.bridge._set_audition_rate(rate)

    def fallback(self, reason):
        if not self.active:
            return
        self.diagnostic = {'cursor': self.timeline.cursor, 'loop': self.loop, 'cache_keys': list(self.cache.data), 'pending': list(self.cache.pending), 'position': self.position()}
        state = {'key': self.source, 'position': self.position(), 'playing': self.playing, 'range': dict(self.range_state)}
        self.timer.stop()
        self.sink.stop()
        self.active = False
        self.reason = reason
        self.last_error = reason
        self.cache.close()
        self.bridge._select_audition_source(state)

    def close(self):
        if getattr(self, '_adapter_closed', False):
            return
        self._adapter_closed = True
        self.timer.stop()
        self.timer.deleteLater()
        if self.active:
            self.sink.stop()
            self.cache.close()
            self.active = False
        if hasattr(self, 'sink'):
            self.sink.deleteLater()
