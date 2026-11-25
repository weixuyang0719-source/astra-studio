"""Bounded asynchronous PCM chunks and sample-accurate shared-source timeline."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading, math
import numpy as np
import soundfile as sf
def _scipy_resampler():
    from scipy.signal import resample_poly
    return resample_poly

def prime_resampler():
    def warm():
        try:
            _scipy_resampler()
        except Exception:
            # This is only prefetch. The real legacy decoder reports exceptions.
            return
    threading.Thread(target=warm, name='astra-scipy-prime', daemon=True).start()
RATE = 48000

def loop_repeats(loop):
    return bool(loop and loop[0] < loop[1])

def loop_position(value, loop, origin=None):
    """Map a target, or a linear elapsed clock from origin, through locators."""
    if not loop or loop[0] == loop[1]:
        return value
    left, right = loop
    if left < right:
        return left + (value - right) % (right - left) if value >= right else value
    if origin is None:
        return left if right <= value < left else value
    if right <= origin < left:
        return left + (value - origin)
    return value + (left - right) if origin < right and value >= right else value


def project_frames(cursor, count, loop):
    """Project frame of each sample in a 1x render, including wraps and skips."""
    pos = cursor + np.arange(count, dtype=np.int64)
    if loop:
        a, z = loop
        if a < z:
            pos = np.where(pos >= z, a + (pos - z) % (z - a), pos)
        elif z <= cursor < a:
            pos = pos + (a - cursor)
        elif cursor < z:
            pos = np.where(pos >= z, pos + (a - z), pos)
    return pos


class ChunkCache:

    def __init__(self, paths, limit=24):
        self.paths = list(map(str, paths))
        self.info = [sf.info(p) for p in self.paths]
        if any((i.channels not in (1, 2) or i.samplerate < 8000 or i.samplerate > 192000 for i in self.info)):
            raise ValueError('Unsupported source format')
        self.limit = limit
        self.data = OrderedDict()
        self.pending = {}
        self.protected = set()
        self.lock = threading.RLock()
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.failures = []

    def request(self, source, second):
        if second < 0:
            return
        key = (source, second)
        with self.lock:
            if self.closed or key in self.data or key in self.pending:
                return
            task = self.pool.submit(self._decode, source, second)
            self.pending[key] = task
            task.add_done_callback(lambda f, k=key: self._done(k, f))

    def _decode(self, source, second):
        info = self.info[source]
        sr = info.samplerate
        halo = 128
        start = max(0, second * sr - halo)
        end = min(info.frames, (second + 1) * sr + halo)
        if start >= info.frames:
            return np.zeros((RATE, 2), np.float32)
        with sf.SoundFile(self.paths[source]) as f:
            f.seek(start)
            a = f.read(end - start, dtype='float32', always_2d=True)
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        if sr != RATE:
            g = math.gcd(sr, RATE)
            alignment = sr // g
            aligned = start - start % alignment
            if aligned != start:
                with sf.SoundFile(self.paths[source]) as f:
                    f.seek(aligned)
                    a = f.read(end - aligned, dtype='float32', always_2d=True)
                if a.shape[1] == 1:
                    a = np.repeat(a, 2, axis=1)
                start = aligned
            a = _scipy_resampler()(a, RATE // g, sr // g, axis=0)
            offset = round((second * sr - start) * RATE / sr)
        else:
            offset = second * sr - start
        result = np.zeros((RATE, 2), np.float32)
        slice_ = a[offset:offset + RATE]
        result[:len(slice_)] = slice_
        return result

    def _done(self, key, future):
        with self.lock:
            self.pending.pop(key, None)
            if self.closed:
                return
            try:
                self.data[key] = future.result()
            except Exception as e:
                self.failures.append(repr(e))
                return
            while len(self.data) > self.limit:
                victim=next((k for k in self.data if k not in self.protected),None)
                if victim is None:break
                del self.data[victim]

    def protect(self, keys):
        with self.lock:
            keys=set(keys)
            if len(keys)>self.limit:raise ValueError('too many protected audio chunks')
            self.protected=keys

    def prefetch(self, frame):
        second = max(0, int(frame) // RATE)
        for source in range(len(self.paths)):
            for offset in range(-1, 4):
                self.request(source, second + offset)

    def read(self, source, positions):
        result = np.zeros((len(positions), 2), np.float32)
        with self.lock:
            for second in np.unique(positions // RATE):
                key = (source, int(second))
                chunk = self.data.get(key)
                if chunk is None:
                    raise BufferError(f'PCM chunk not ready: {key}')
                mask = positions // RATE == second
                result[mask] = chunk[positions[mask] % RATE]
                self.data.move_to_end(key)
        return result

    @property
    def bytes(self):
        with self.lock:
            return sum((a.nbytes for a in self.data.values()))

    def close(self, wait=False):
        with self.lock:
            self.closed = True
            self.data.clear()
        self.pool.shutdown(wait=wait, cancel_futures=True)

class Timeline:

    def __init__(self, cache):
        self.cache = cache
        self.cursor = 0
        self.source = 0
        self.old = 0
        self.fade_at = 240
        self._loop = None
        self.lock = threading.RLock()
        self.failed = None

    @property
    def loop(self):
        with self.lock:
            return self._loop

    @loop.setter
    def loop(self, value):
        with self.lock:
            if value is not None and (min(value) < 0 or value[1] == value[0]):
                raise ValueError('invalid loop')
            self._loop = value

    def select(self, key):
        with self.lock:
            if not 0 <= key < len(self.cache.paths):
                raise ValueError('unknown source')
            self.old = self.source
            self.source = key
            self.fade_at = 0

    def seek(self, frame):
        with self.lock:
            self.cursor = max(0, int(frame))
            self.fade_at = 240
            self.cache.prefetch(self.cursor)

    def render(self, count):
        with self.lock:
            pos = project_frames(self.cursor, count, self.loop)
            result = self.cache.read(self.source, pos)
            if self.fade_at < 240:
                n = min(count, 240 - self.fade_at)
                old = self.cache.read(self.old, pos[:n])
                weight = ((self.fade_at + np.arange(n) + 1) / 240).astype(np.float32)[:, None]
                result[:n] = old + (result[:n] - old) * weight
                self.fade_at += n
            self.cursor = int(pos[-1]) + 1 if count else self.cursor
            return result

    def set_rate(self, rate):
        if float(rate) != 1:
            raise NotImplementedError('Use existing pitch-preserving playback backend for non-1x rates')
