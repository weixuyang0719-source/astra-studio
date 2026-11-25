"""DAW-style output device manager (R12). GUI-thread owned; the audio owner thread only reports.

Lifecycle (design after Tracktion DeviceManager load/saveSettings + audioDeviceAboutToStart/Stopped,
and Ardour PortAudioBackend stop -> open -> read back actual parameters; no code copied):

  request(setup)    validate against the read-only catalog; ASIO additionally needs confirm_open=True
  begin()           remember previous setup (and the caller keeps position, pauses playback)
  device_factory()  what the new owner opens; it records the ACTUAL parameters it got
  opened(meta)      success: setup becomes last_good and is saved (Astra's own file only)
  failed(error)     rollback to last_good; the caller re-creates the previous output, paused
  endpoints_changed(ids)  preferred device gone -> fallback (auto); back again -> restore (paused)

Never changes system/driver settings (sample rate, buffer, default device). Default setup is 'auto',
which is exactly the pre-R12 behaviour (RAW preferred -> native-shared -> ordinary Qt).
"""
import json, threading, time
from dataclasses import dataclass, asdict, replace
from pathlib import Path
from . import device_catalog

ENGINE_RATE = 48000


@dataclass(frozen=True)
class DeviceSetup:
    backend: str = 'auto'
    device: str | None = None       # WASAPI endpoint id, or ASIO driver name
    sample_rate: int = ENGINE_RATE
    buffer_frames: int | None = None  # ASIO only (driver preferred when None); WASAPI shared uses the engine period

    def key(self):
        return (self.backend, self.device, self.sample_rate, self.buffer_frames)


class AudioDeviceManager:
    def __init__(self, path=None, catalog=None, native_factory=None, asio_factory=None):
        self.path = Path(path) if path else None
        self._catalog = catalog or device_catalog.catalog
        self._manual_asio_setup = None
        self._manual_asio_route = None
        self.reused_open_asio = False
        self._native_factory = native_factory
        self._asio_factory = asio_factory
        self.current = DeviceSetup()
        self.last_good = DeviceSetup()
        self.preferred = DeviceSetup()   # what the user chose; may be temporarily unavailable
        self.previous = None
        self.state = 'idle'               # idle | switching | active | rolled-back | device-missing
        self.actual = None
        self.message = ''
        self.events = []
        self._lock = threading.Lock()
        self._opened = None
        self._reopened = False
        self.remembered = None
        self._load()

    @property
    def awaiting_asio_confirmation(self):
        return self.remembered is not None

    # ---- persistence (Astra's own settings file) ---------------------------------------
    def _load(self):
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text('utf8'))
            setup = DeviceSetup(**{k: data['setup'][k] for k in asdict(DeviceSetup()) if k in data.get('setup', {})})
            if setup.backend in device_catalog.BACKENDS:
                # Unified ASIO policy: saved ASIO resumes directly without an extra confirmation step.
                self.current = self.last_good = self.preferred = setup
        except Exception as exc:
            self.events.append(('load_failed', repr(exc)))

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'version': 1, 'setup': asdict(self.preferred), 'last_good': asdict(self.last_good)}, ensure_ascii=False), 'utf8')
            tmp.replace(self.path)
        except Exception as exc:
            self.events.append(('save_failed', repr(exc)))

    # ---- catalog / validation -----------------------------------------------------------
    def listing(self):
        return dict(catalog=self._catalog(), **self.status())

    @property
    def manual_asio_route(self):
        if self._manual_asio_setup == self.current:
            return self._manual_asio_route
        return None

    def status(self):
        # Status consumers must not refresh the settings dropdown inventory.
        return dict(current=asdict(self.current), preferred=asdict(self.preferred), last_good=asdict(self.last_good),
                    state=self.state, actual=self.actual, message=self.message, engine_rate=ENGINE_RATE,
                    remembered=asdict(self.remembered) if self.remembered else None)

    def validate(self, setup, confirm_open=False, *, target_catalog=None):
        if setup.buffer_frames is not None and (type(setup.buffer_frames) is not int or not 1 <= setup.buffer_frames <= 65536):
            return '缓冲帧数须为 1–65536 的整数，或选择驱动默认。'
        if setup.backend not in device_catalog.BACKENDS:
            return '未知的音频后端。'
        if setup.sample_rate != ENGINE_RATE:
            return f'目前引擎固定以 {ENGINE_RATE} Hz 渲染；共享模式由系统自动转换，ASIO 需设备工作在 {ENGINE_RATE} Hz。'
        cat = target_catalog if target_catalog is not None else self._catalog()
        if setup.backend in ('auto', 'compat') and (setup.device or setup.buffer_frames):
            return '自动与兼容输出不指定具体设备或缓冲。'
        if setup.backend in ('wasapi-shared', 'wasapi-raw'):
            if setup.buffer_frames is not None:
                return 'WASAPI 共享模式使用系统引擎周期，不能单独设置缓冲。'
            if setup.device and not any(e.get('id') == setup.device for e in cat['wasapi']):
                return '找不到该输出设备（可能已拔出）。'
        if setup.backend == 'asio':
            row = next((d for d in cat['asio'] if d['name'] == setup.device), None)
            if row is None:
                return '找不到该 ASIO 驱动。'
            if row['state'] != 'registered':
                return f'ASIO 驱动不可用：{row["state"]}。'
            if not cat['asio_runtime']['available']:
                return cat['asio_runtime']['note']
            if not confirm_open:
                return '打开 ASIO 驱动可能占用声卡或影响正在使用它的程序，需要明确确认后才会尝试。'
        return None

    # ---- switching ----------------------------------------------------------------------
    def request(self, setup, confirm_open=False, *, from_settings=False, reuse_asio=None):
        target_catalog = None
        if from_settings:
            if setup.backend == 'asio':
                row = device_catalog.asio_driver(setup.device)
                target_catalog = dict(asio=[row] if row is not None else [],
                                      asio_runtime=device_catalog.asio_runtime())
            elif setup.backend in ('wasapi-shared', 'wasapi-raw') and setup.device:
                # Validation of an explicitly selected WASAPI endpoint needs its own IDs only.
                target_catalog = dict(wasapi=device_catalog.wasapi_endpoints())
            else:
                target_catalog = {}
        error = self.validate(setup, confirm_open) if target_catalog is None else self.validate(
            setup, confirm_open, target_catalog=target_catalog)
        if error:
            self.message = error
            return error
        route = None
        if target_catalog is not None and setup.backend == 'asio':
            from .vst_project import device_route
            row = next(d for d in target_catalog['asio'] if d['name'] == setup.device)
            try:
                route = device_route(('asio', row), setup.sample_rate, setup.buffer_frames or 0, [0, 1])
            except ValueError as exc:
                self.message = str(exc)
                return self.message
            route['asio_candidates'] = (setup.device,)
        actual = reuse_asio(route) if route is not None and reuse_asio is not None else None
        with self._lock:
            self.reused_open_asio = actual is not None
            self._manual_asio_setup = setup if route is not None else None
            self._manual_asio_route = route
            self.remembered = None
            self.previous = self.current
            self.current = self.preferred = setup
            self.state = 'switching'
            self.actual = None
            self._opened = None
            self.message = '正在停止旧输出并打开新设备…'
        if actual is not None:
            # The old stream is also the requested verified stream. Preserve its owner,
            # transport and plugin graph; update only selection/status and persistence.
            self.events.append(('reuse-open-asio', setup.key(), time.monotonic()))
            with self._lock:
                self._opened = actual
            self.poll()
            return None
        self.events.append(('switch', setup.key(), time.monotonic()))
        if setup.backend in ('auto', 'compat'):
            # Routings without a device of their own to confirm: they are safe states.
            self.state, self.last_good = 'active', setup
            self.message = '已切换为自动输出（RAW 优先，否则原生共享）。' if setup.backend == 'auto' else '已切换为兼容输出（Qt 媒体播放）。'
            self._save()
        return None

    def device_factory(self):
        """Factory for the NEXT owner, or None for the default 'auto' routing."""
        setup = self.current
        if setup.backend in ('auto', 'compat'):
            return None

        def opened(device):
            # The device's own dict (not a copy): live fields such as the observed callback size
            # arrive after the first driver callback.
            meta = getattr(device, 'meta', None)
            if not isinstance(meta, dict):
                meta = {}
            meta.setdefault('backend', setup.backend)
            meta['frames'] = getattr(device, 'capacity', meta.get('host_frames'))
            with self._lock:
                if self.current == setup:
                    self._opened = meta
            return device

        if setup.backend in ('wasapi-shared', 'wasapi-raw'):
            make = self._native_factory
            if make is None:
                from .raw_transport_owner import NativeRaw
                make = lambda endpoint, raw: NativeRaw(endpoint=endpoint, raw=raw)
            raw = setup.backend == 'wasapi-raw'
            return lambda: opened(make(setup.device, raw))
        make = self._asio_factory
        if make is None:
            from .asio_stream import asio_stream_factory
            from .callback_device import CallbackRingDevice
            make = lambda name, block: CallbackRingDevice(asio_stream_factory(name, block), label='asio')
        return lambda: opened(make(setup.device, setup.buffer_frames))

    def poll(self):
        """GUI thread: consume the owner's open report."""
        with self._lock:
            meta, self._opened = self._opened, None
        if meta is not None and self.state in ('switching', 'active', 'idle'):
            self.actual = meta
            self.state = 'active'
            self.last_good = self.current
            self._reopened = False
            self.message = '已打开：' + describe(meta)
            self._save()
        return meta

    def confirm_kept_switch(self, requested, actual, *, previous=None, error=None):
        # Publish only after verified endpoint callback.
        with self._lock:
            if self.current != requested:
                return False
            if previous is not None:
                self.current = self.preferred = previous
                self._manual_asio_setup = self._manual_asio_route = None
            self._opened = actual
        self.poll()
        if previous is not None:
            self.state = 'rolled-back'
            self.message = '无法打开所选驱动，已恢复原输出：' + short(error)
        return True

    def failed(self, error):
        """Report failure without rewriting the user's selected output preference."""
        self.events.append(('failed', self.current.key(), str(error)[:200]))
        self.state = 'failed'
        self.actual = None
        self.message = 'Selected audio output failed; selection preserved. ' + short(error)
        return self.current

    def endpoints_changed(self, endpoint_ids):
        """Hot-plug. Returns the setup the bridge should now use (always paused by the caller)."""
        want = self.preferred
        if want.backend in ('wasapi-shared', 'wasapi-raw') and want.device:
            present = want.device in endpoint_ids
            if not present and self.current == want:
                self.current = DeviceSetup()
                self.state = 'device-missing'
                self.message = '所选输出设备已断开，暂用系统默认输出；重新连接后会恢复（需点击播放）。'
                return self.current
            if present and self.current != want:
                self.current = want
                self.state = 'switching'
                self.message = '所选输出设备已重新连接，已恢复（需点击播放）。'
                return self.current
        return None


def short(error):
    text = str(error)
    if '0x88890027' in text: return '设备不支持 RAW 属性'
    if 'in use' in text.lower() or '占用' in text: return '设备被其他程序占用'
    if 'Hz' in text: return text[:80]
    return text[:120]


def describe(meta):
    """Read-back parameters, kept apart (none of this is a low-latency claim):
    driver/hardware callback size, Astra's queue (ring or WASAPI shared buffer), the device's own
    reported output latency, and the resulting end-to-end upper bound."""
    meta = dict(meta)  # Normalize actual native read-back without modifying owner data.
    if meta.get('rate') is None and meta.get('sample_rate') is not None:
        meta['rate'] = meta['sample_rate']
    if meta.get('observed_block') is None and meta.get('observed_frames') is not None:
        meta['observed_block'] = meta['observed_frames']
    if meta.get('latency_ms') is None and meta.get('output_latency') is not None:
        meta['latency_ms'] = round(float(meta['output_latency']) * 1000, 3)
    rate = meta.get('rate') or ENGINE_RATE
    ms = lambda frames: frames / rate * 1000
    parts = [str(meta.get('device') or '')]
    if meta.get('rate'): parts.append(f'{meta["rate"]} Hz')
    if meta.get('backend') == 'asio' or meta.get('ring_frames'):
        if meta.get('observed_block'): parts.append(f'驱动回调 {meta["observed_block"]} 帧（{ms(meta["observed_block"]):.1f} ms）')
        ring = meta.get('ring_frames')
        if ring: parts.append(f'Astra 排队上限 {ring} 帧（{ms(ring):.1f} ms）')
        if meta.get('latency_ms'): parts.append(f'设备输出延迟 {meta["latency_ms"]} ms')
        if ring and meta.get('latency_ms'): parts.append(f'端到端上限约 {ms(ring) + float(meta["latency_ms"]):.0f} ms')
    else:
        frames = meta.get('host_frames') or meta.get('block')
        if frames: parts.append(f'共享缓冲 {frames} 帧（{ms(frames):.1f} ms，另加系统混音与设备延迟）')
    before = meta.get('driver_rate_before')
    if before and meta.get('rate') and before != meta['rate']: parts.append(f'驱动打开前报告 {before} Hz')
    return ' · '.join(p for p in parts if p)
