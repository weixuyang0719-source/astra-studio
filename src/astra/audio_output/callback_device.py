"""Pull-model (callback) output behind the existing push-device interface (R12).

PauseGuardTransport drives devices through capacity/padding/write/start/stop/reset/close (the
WASAPI-shared NativeRaw contract). ASIO-style drivers instead call back on their own thread for each
block. CallbackRingDevice bridges the two with a single-producer/single-consumer ring:

* producer = the transport owner thread (render, fades, PauseGuard timing all unchanged);
* consumer = the driver callback, which ONLY copies ring -> output and zero-fills. It never decodes,
  touches files/network, takes a blocking lock, allocates large buffers or waits for the UI.
* stop() keeps the driver running and makes the callback emit silence (a warm stream, no driver
  restart per pause); reset() drops queued frames while stopped.

Errors (driver reset request, device lost, xrun overload) are latched and raised from padding()/write()
on the owner thread, so the existing Handoff error -> fallback path closes it and reopens a safe output.
Sample rate: the timeline renders 48 kHz; a stream that does not run at 48 kHz is refused at open
(no resampler in this round) and the manager rolls back.
"""
import numpy as np
from .raw_transport_owner import BackendInitializationError, LateRelease, RATE


class CallbackRingDevice:
    def __init__(self, stream_factory, rate=RATE, capacity=None, channels=2, label='callback'):
        self.error = None
        self.running = False
        self.underruns = 0
        self.xruns = 0
        self._r = 0
        self._w = 0
        self._channels = channels
        self.closed = False
        self._stream = None
        self._observed = None
        self.meta = {}
        try:
            self._stream = stream_factory(self._callback)
            if hasattr(self._stream, 'on_error'):
                self._stream.on_error = self.fail
            actual = dict(getattr(self._stream, 'actual', {}) or {})
        except BackendInitializationError:
            raise  # keeps its own release knowledge (incl. LateRelease)
        except BaseException as exc:
            # The factory raised before returning a stream. Stream factories release their own
            # partial opens before raising; a factory may say otherwise via output_released.
            raise BackendInitializationError(repr(exc), output_released=getattr(exc, 'output_released', True)) from exc
        if int(actual.get('rate') or 0) != rate:
            self._refuse(f'device runs at {actual.get("rate")} Hz; Astra renders {rate} Hz (no resampler yet)')
        block = int(actual.get('block') or 0)
        if block <= 0:
            self._refuse('device reported no buffer size')
        self.capacity = int(capacity or max(2048, 4 * block))
        self._ring = np.zeros((self.capacity, channels), np.float32)
        self.meta = dict(actual, backend=label, ring_frames=self.capacity, host_frames=self.capacity)
        self._started = False

    # ---- driver thread ---------------------------------------------------------------
    def _callback(self, out, frames, status=None):
        if self._observed is None:
            self._observed = frames
            self.meta['observed_block'] = frames  # effective callback size, as the driver actually runs
        if status:
            self.xruns += 1
        if not self.running:
            out[:frames] = 0
            return
        r = self._r
        n = min(self._w - r, frames)
        if n:
            at = r % self.capacity
            first = min(n, self.capacity - at)
            out[:first] = self._ring[at:at + first]
            if n > first:
                out[first:n] = self._ring[:n - first]
            self._r = r + n  # publish after the copy
        if n < frames:
            out[n:frames] = 0
            self.underruns += 1

    def fail(self, message):
        """Called by the stream wrapper (driver reset/lost). Latched for the owner thread."""
        if self.error is None:
            self.error = str(message)

    # ---- owner thread (NativeRaw contract) ----------------------------------------------
    def _check(self):
        if self.error:
            raise RuntimeError('audio device: ' + self.error)

    def padding(self):
        self._check()
        return self._w - self._r

    def write(self, pcm):
        self._check()
        n = len(pcm)
        if self._w - self._r + n > self.capacity:
            raise BufferError('ring overfill')
        at = self._w % self.capacity
        first = min(n, self.capacity - at)
        self._ring[at:at + first] = pcm[:first]
        if n > first:
            self._ring[:n - first] = pcm[first:]
        self._w += n  # publish after the copy

    def start(self):
        self._check()
        if not self._started:
            self._stream.start()
            self._started = True
        self.running = True

    def stop(self):
        self.running = False

    def reset(self):
        if self.running:
            raise RuntimeError('reset while running')
        self._r = self._w

    def _refuse(self, message):
        # "Released" only if the close really succeeded (or will, for a late close on the ASIO thread).
        try:
            self._safe_close()
        except LateRelease as late:
            raise LateRelease(message + '; close still pending', late.release_event) from late
        except BaseException as exc:
            raise BackendInitializationError(message + '; close failed: ' + repr(exc), output_released=False) from exc
        raise BackendInitializationError(message, output_released=True)

    def _safe_close(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
            finally:
                s.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.running = False
        self._safe_close()
