"""ASIO stream factory via PortAudio (python-sounddevice, MIT; PortAudio, MIT) — R12, NOT yet run on hardware.

Only constructed after an explicit, user-confirmed device switch (AudioDeviceManager.request with
confirm_open=True). Importing sounddevice with SD_ENABLE_ASIO initialises PortAudio's ASIO host API,
which loads every registered ASIO driver; that is why discovery (device_catalog) never imports it.
The ASIO-enabled PortAudio binary contains Steinberg ASIO SDK code: redistributing Astra with it needs
the ASIO SDK proprietary licence or GPLv3 terms for Astra (Steinberg dual licence, 2025-10).
Ardour/JUCE patterns used as design only: read back the stream's actual rate/latency after open
(Ardour PortAudioIO::set_sample_rate_and_latency_from_stream), treat driver reset/abort as a device
change that is handled off the audio thread (JUCE ASIOAudioIODevice::resetRequest -> timer).
"""
import queue, threading
from .raw_transport_owner import BackendInitializationError, LateRelease


RATES = (44100, 48000, 88200, 96000)


def runtime_report(sd, driver_name=None):
    """What the DEPLOYED PortAudio actually loaded (not what the wheel or registry claims):
    library path/version, host APIs, and per ASIO driver: channels, driver-reported rate, supported
    rates (query only) and the driver's buffer range (PaAsio_GetAvailableBufferSizes, query only)."""
    import ctypes
    lib = getattr(sd, '_libname', None)
    report = dict(dll=lib, version=sd.get_portaudio_version()[1], hostapis=[h['name'] for h in sd.query_hostapis()], drivers=[])
    asio = [h for h in sd.query_hostapis() if 'ASIO' in h['name']]
    report['asio_loaded'] = bool(asio)
    if not asio:
        return report
    try:
        fn = ctypes.CDLL(lib).PaAsio_GetAvailableBufferSizes
        fn.argtypes = [ctypes.c_int] + [ctypes.POINTER(ctypes.c_long)] * 4; fn.restype = ctypes.c_int
    except Exception:
        fn = None
    for index in asio[0]['devices']:
        d = sd.query_devices(index)
        if driver_name and d['name'] != driver_name:
            continue
        row = dict(name=d['name'], index=index, outputs=d['max_output_channels'], inputs=d['max_input_channels'], driver_rate=int(d['default_samplerate']))
        if fn is not None:
            v = [ctypes.c_long() for _ in range(4)]
            err = fn(index, *[ctypes.byref(x) for x in v])
            row['buffer'] = dict(min=v[0].value, max=v[1].value, preferred=v[2].value, granularity=v[3].value) if err == 0 else dict(error=err)
        rates = {}
        for r in RATES:
            try:
                sd.check_output_settings(device=index, samplerate=r, channels=2, dtype='float32'); rates[r] = True
            except Exception:
                rates[r] = False
        row['rates'] = rates
        report['drivers'].append(row)
    return report


class _AsioHost:
    """One long-lived thread owns PortAudio's ASIO host API: initialisation, queries and every
    open/start/stop/close. ASIO drivers are apartment-threaded COM objects; initialising PortAudio on
    one thread and opening on another failed on the real Focusrite driver ("Unanticipated host
    error"). JUCE likewise drives ASIO from one (message) thread. The audio callback still runs on
    the driver's own thread and never comes here. The loop pumps window messages for drivers that
    post them (STA)."""
    _lock = threading.Lock()
    _instance = None

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, name='Astra-ASIO-host', daemon=True)
        self.thread.start()

    @staticmethod
    def _pump():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg)); user32.DispatchMessageW(ctypes.byref(msg))

    def _run(self):
        import ctypes
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        while True:
            try:
                task = self.queue.get(timeout=0.02)
            except queue.Empty:
                try:
                    self._pump()
                except Exception:
                    pass
                continue
            self.run_task(task)

    @staticmethod
    def run_task(task):
        with task['lock']:
            if task['state'] == 'cancelled':
                return  # never started: nothing was opened
            task['state'] = 'running'
        value = error = None
        try:
            value = task['fn']()
        except BaseException as exc:
            error = exc
        with task['lock']:
            abandoned = task['state'] == 'abandoned'
            task['state'] = 'done'
            task['value'], task['error'] = value, error
        if not abandoned:
            task['done'].set()
            return
        # The caller gave up while we were working. Whatever we got belongs to nobody: close it
        # HERE (same ASIO thread), and only then report the release. A failed close keeps it unknown.
        if error is None and value is not None and task['cleanup'] is not None:
            try:
                task['cleanup'](value)
            except BaseException as exc:
                task['cleanup_error'] = exc
                return
        task['released'].set()

    def call(self, fn, timeout=20, cleanup=None):
        task = dict(fn=fn, cleanup=cleanup, state='queued', lock=threading.Lock(), done=threading.Event(),
                    released=threading.Event(), value=None, error=None, cleanup_error=None)
        if threading.current_thread() is self.thread:
            self.run_task(task)
        else:
            self.queue.put(task)
            if not task['done'].wait(timeout):
                with task['lock']:
                    if task['state'] == 'queued':
                        task['state'] = 'cancelled'
                        raise BackendInitializationError('ASIO host busy: request cancelled before it started', output_released=True)
                    if task['state'] == 'running':
                        task['state'] = 'abandoned'
                        raise LateRelease('ASIO driver did not answer in time; the late result is closed on the ASIO thread', task['released'])
                # finished between the timeout and taking the lock: use the result
        if task['error'] is not None:
            raise task['error']
        return task['value']


def _sounddevice():
    import os
    os.environ.setdefault('SD_ENABLE_ASIO', '1')
    import sounddevice as sd
    return sd


def asio_runtime_report(driver_name=None):
    """Runtime report from the ASIO host thread (loads the drivers PortAudio enumerates)."""
    return _AsioHost.get().call(lambda: runtime_report(_sounddevice(), driver_name))


class AsioStream:
    def __init__(self, driver_name, callback, rate=48000, block=None):
        self._host = _AsioHost.get()
        self._s = None
        # A timed-out open is closed by the host thread when it finally returns (LateRelease).
        self._host.call(lambda: self._open(driver_name, callback, rate, block) or self,
                        cleanup=lambda stream: stream._s is not None and stream._s.close())

    def _open(self, driver_name, callback, rate, block):
        sd = _sounddevice()
        report = runtime_report(sd, driver_name)
        if not report['asio_loaded']:
            raise RuntimeError(f'deployed PortAudio has no ASIO host API at runtime: {report["dll"]}')
        hostapis = [h for h in sd.query_hostapis() if 'ASIO' in h['name']]
        index = next((d for d in hostapis[0]['devices'] if sd.query_devices(d)['name'] == driver_name and sd.query_devices(d)['max_output_channels'] >= 2), None)
        if index is None:
            raise RuntimeError(f'ASIO driver not found or has no stereo output: {driver_name}')
        # What the driver reported before we opened at 48 kHz. Opening at another rate may switch
        # the interface clock; the value is surfaced in the UI instead of hidden.
        driver_rate = float(sd.query_devices(index)['default_samplerate'])
        self.on_error = None

        def cb(outdata, frames, _time, status):
            callback(outdata, frames, bool(status))  # copy-only callback; see CallbackRingDevice

        def finished():
            if self.on_error and not self._closing:
                self.on_error('ASIO stream stopped by driver (reset request, device lost or in use)')
        self._closing = False
        self._s = sd.OutputStream(device=index, samplerate=rate, blocksize=block or 0, channels=2, dtype='float32',
                                  latency='low', callback=cb, finished_callback=finished)
        try:
            self._describe(rate, driver_name, driver_rate, report)
        except BaseException:
            s, self._s = self._s, None
            s.close()  # opened but unusable: release before reporting failure
            raise

    def _describe(self, rate, driver_name, driver_rate, report):
        latency = float(self._s.latency)
        self.actual = dict(rate=int(self._s.samplerate), block=int(self._s.blocksize or max(64, round(latency * rate / 2))),
                           latency_ms=round(latency * 1000, 2), device=driver_name, backend='asio',
                           driver_rate_before=int(driver_rate), configured_block=int(self._s.blocksize),
                           portaudio_dll=report['dll'], portaudio_version=report['version'], hostapis=report['hostapis'],
                           driver=(report['drivers'] or [{}])[0])

    def start(self):
        self._host.call(self._s.start)

    def stop(self):
        self._closing = True
        self._host.call(self._s.stop)

    def close(self):
        # A close that times out raises LateRelease; the host still finishes it and then signals.
        self._closing = True
        self._host.call(self._s.close)


def asio_stream_factory(driver_name, block=None):
    return lambda callback: AsioStream(driver_name, callback, block=block)
