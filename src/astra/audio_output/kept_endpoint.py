"""Control bindings for the single physical ASIO stream, VST producers, and direct Tracks PCM lane."""
import ctypes as C
from pathlib import Path
import threading
from time import monotonic
from uuid import uuid4
from .native_host import AsioControlInfo, HostRoute, HostSnapshot, asio_open_parameters

P, I, U, Q = C.c_void_p, C.c_int, C.c_uint32, C.c_uint64
def functions(lib, definitions):
    for name, result, args in definitions:
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    return lib

def mapping_library():
    path = Path(__file__).with_name('_native') / 'astra_kept_endpoint.dll'
    return functions(C.CDLL(str(path)), [
        ('kept_map', P, [C.c_wchar_p, I]), ('kept_unmap', None, [P]),
        ('kept_stat', Q, [P, U]), ('kept_request', Q, [P, Q]),
        ('kept_forget_retired', None, [P, Q]),
        ('kept_rate', None, [P, U]),
        ('kept_producer_v2', P, [P, Q, P, P, P, P, P, U]),
        ('kept_stop', None, [P]), ('kept_destroy', I, [P]),
        ('kept_producer_state', U, [P]),
        ('kept_direct_attach', I, [P, P, P, P, P]),
        ('kept_direct_detach', I, [P]),
        ('kept_direct_mode', None, [P, U]),
        ('kept_direct_mode_get', U, [P])])

def record(cls):
    return cls(size=C.sizeof(cls), version=1)

class _MemoryStatus(C.Structure):
    _fields_ = [('length', U), ('load', U), ('total', Q), ('available', Q), ('other', Q * 5)]


def standby_memory_available():
    """Only keep an idle sampler when Windows has physical memory headroom."""
    status = _MemoryStatus()
    status.length = C.sizeof(status)
    return bool(C.windll.kernel32.GlobalMemoryStatusEx(C.byref(status)) and
                status.available >= max(1024 ** 3, status.total // 10))


def _snapshot_to_dict(s):
    return {k: (getattr(s, k).decode('utf8', errors='replace') if isinstance(getattr(s, k), bytes) else getattr(s, k))
            for k, _ in s._fields_}


def _route_to_dict(host, handle, r):
    result = {k: (getattr(r, k).decode('utf8', errors='replace') if isinstance(getattr(r, k), bytes) else getattr(r, k))
              for k, _ in r._fields_}
    getter = getattr(host, 'astra_host_asio_control', None)
    if getter and handle and r.host_type == 3:
        try:
            getter.argtypes = [P, C.POINTER(AsioControlInfo)]
            getter.restype = I
            info = record(AsioControlInfo)
            if getter(handle, C.byref(info)) and info.state == 2 and not info.error:
                result['buffer_info'] = {key: getattr(info, key) for key in ('min_frames', 'max_frames', 'preferred_frames', 'granularity')}
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    return result


class KeptEndpoint:
    def __init__(self, libraries, device):
        self._lock = threading.RLock()
        self._libraries = libraries
        self.device = {k: device[k] for k in ('backend','sample_rate','output_channels','native_selector','device_id','buffer_frames')}
        if 'asio_candidates' in device:
            self.device['asio_candidates'] = tuple(device['asio_candidates'])
        self.kind = {'asio':3, 'wasapi-shared':13}[device['backend']]
        raw_candidates = list(device.get('asio_candidates') or (device['native_selector']['exact_device'],))
        self._candidates = [c for c in raw_candidates if isinstance(c, str) and c.strip()] or [device['native_selector']['exact_device']]
        self._candidate_index = 0
        self._candidate_errors = []
        self._restarting_host = False
        self._awaiting_callback_since = None
        self._open_failure = None
        self._open_stage = None
        self._open_stage_started = 0.
        self._select_candidate(0)
        self.name = 'Local\\AstraKept-' + uuid4().hex
        self.mapping = mapping_library()
        self.host = functions(C.CDLL(str(libraries.device_library)), [
            ('astra_host_create', P, [C.c_wchar_p,P,P]),
            ('astra_host_open_backend', I, [P,I,C.c_char_p,C.c_double,U]),
            ('astra_host_snapshot', I, [P,C.POINTER(HostSnapshot)]),
            ('astra_host_route', I, [P,C.POINTER(HostRoute)]),
            ('astra_host_close', I, [P]), ('astra_host_destroy', I, [P])])
        self.mem = self.mapping.kept_map(self.name, 1)
        if not self.mem: raise OSError('无法建立共享音频输出缓冲。')
        self.mapping.kept_rate(self.mem,round(device['sample_rate']))
        self.handle = self._create_host_handle()
        if not self.handle:
            self.mapping.kept_unmap(self.mem)
            self.mem = None
            raise OSError('无法启动所选音频设备。')
        self.open_sent = self.closing = False
        self.info = None
        self._driver_parameters = None
        self._last_host_snapshot = None
        self._last_route_dict = None
        self._direct_core = None
        self.generation = 0

    def _select_candidate(self, index):
        self._candidate_index = index
        chosen = self._candidates[index]
        self.device['native_selector'] = dict(exact_device=chosen)
        if self.kind == 3:
            self.device['device_id'] = 'asio:' + chosen

    def _create_host_handle(self):
        return self.host.astra_host_create(
            str(self._libraries.portaudio_library),
            C.cast(self.mapping.kept_callback, P),
            self.mem,
        )

    def matches(self, device):
        with self._lock:
            if not isinstance(device, dict):
                return False
            if self.closing or not self.handle:
                return False
            base_keys = ('backend', 'sample_rate', 'output_channels', 'buffer_frames')
            if not all(device.get(k) == self.device.get(k) for k in base_keys):
                return False
            active_name = (self.info or {}).get('device') or self.device['native_selector']['exact_device']
            if device.get('native_selector') == self.device.get('native_selector') and device.get('device_id') == self.device.get('device_id'):
                return True
            # A policy route carrying an ordered candidate list matches the already-open
            # physical/fallback ASIO driver if that active driver is in the candidate list.
            candidates = device.get('asio_candidates')
            if self.kind == 3 and candidates and active_name in candidates:
                return True
            return False

    def reuse_open_asio(self, device, *, require_requested_frames=True):
        """Return proven actual settings for policy-only apply, never open/restart a host."""
        with self._lock:
            if (self.kind != 3 or self.closing or not self.handle or not self.mem
                    or self._restarting_host or self.info is None or not self.matches(device)
                    or self.info['device'] != device['native_selector']['exact_device']):
                return None
            s = record(HostSnapshot)
            if (not self.host.astra_host_snapshot(self.handle, C.byref(s))
                    or s.released or s.state != 3 or s.error or not s.callbacks
                    or not s.observed_frames or s.observed_frames != self.stat(4) or self.stat(5)
                    or s.sample_rate != device['sample_rate']
                    or s.device.decode('utf8', 'replace') != self.info['device']):
                return None
            wanted_block = device['buffer_frames']
            if require_requested_frames and wanted_block and wanted_block != s.observed_frames:
                return None
            return dict(backend='asio', device=self.info['device'], rate=s.sample_rate,
                        observed_block=s.observed_frames, output_latency=s.output_latency,
                        native_core=True, shared=False, raw=False)

    def stat(self, which):
        with self._lock:
            return self.mapping.kept_stat(self.mem, which) if self.mem else 0

    def request(self, token):
        with self._lock:
            return self.mapping.kept_request(self.mem, token) if self.mem else 0

    def forget_retired(self, route):
        """Drop only a confirmed retired writer's own generation on this live mapping."""
        with self._lock:
            if not route.released or not self.mem or not self.handle or self.closing:
                return False
            lease = route.device.get('kept_endpoint')
            if not lease or lease.get('name') != self.name:
                return False
            token = lease.get('token')
            if type(token) is not int or not 0 < token < (1 << 64) - 1:
                return False
            self.mapping.kept_forget_retired(self.mem, token)
            return True

    def set_direct_mode(self, enabled):
        with self._lock:
            if self.mem:
                self.mapping.kept_direct_mode(self.mem, 1 if enabled else 0)

    def direct_mode(self):
        with self._lock:
            return bool(self.mapping.kept_direct_mode_get(self.mem)) if self.mem else False

    def attach_direct(self, core):
        with self._lock:
            if not self.mem or not self.handle or self.closing:
                raise RuntimeError('共享音频端点不可用。')
            with core._lock:
                if not core.handle:
                    raise RuntimeError('core closed')
                ok = self.mapping.kept_direct_attach(
                    self.mem,
                    core.callback_address,
                    core.handle,
                    C.cast(core.lib.astra_core_retain, P),
                    C.cast(core.lib.astra_core_release, P),
                )
            if not ok:
                raise RuntimeError('分轨 PCM 通道已被占用，无法挂载。')
            self._direct_core = core

    def detach_direct(self, core=None):
        with self._lock:
            if core is not None and self._direct_core is not None and self._direct_core is not core:
                return True
            if not self.mem:
                self._direct_core = None
                return True
            done = bool(self.mapping.kept_direct_detach(self.mem))
            if done:
                self._direct_core = None
            return done

    def snapshot_dict(self):
        with self._lock:
            s = self._last_host_snapshot
            if s is None:
                return {'state': 0, 'released': not bool(self.handle), 'error': 0, 'message': '', 'device': self.device['native_selector']['exact_device'], 'sample_rate': self.device['sample_rate'], 'observed_frames': 0, 'output_latency': 0.0}
            return _snapshot_to_dict(s)

    def route_dict(self):
        with self._lock:
            if self._last_route_dict is not None:
                return dict(self._last_route_dict)
            if not self.handle:
                raise RuntimeError('route unavailable')
            r = record(HostRoute)
            if not self.host.astra_host_route(self.handle, C.byref(r)):
                raise RuntimeError('route unavailable')
            self._last_route_dict = _route_to_dict(self.host, self.handle, r)
            return dict(self._last_route_dict)

    def diagnostic_state(self):
        """Read existing atomic counters on the control thread, after poll."""
        with self._lock:
            snapshot = self._last_host_snapshot
            if snapshot is None or not self.mem:
                return None
            return dict(stream_state=snapshot.state, host_error=snapshot.error, callbacks=snapshot.callbacks,
                        driver_flags=snapshot.status_flags, frames=snapshot.observed_frames,
                        selected_token=self.stat(0), cursor_frames=self.stat(1), dropped_blocks=self.stat(2),
                        wrong_size=self.stat(5), audible_frames=self.stat(6), silent_tail_frames=self.stat(7),
                        direct_mode=self.direct_mode())

    def driver_parameters(self):
        # Pure status read: never start, poll, or touch the native owner here.
        with self._lock:
            s = self._last_host_snapshot
            if (self.handle and not self.closing and s is not None
                    and s.state in (2,3) and not s.released and not s.error
                    and s.sample_rate == self.device['sample_rate']
                    and s.device.decode('utf8','replace') == self.device['native_selector']['exact_device']):
                return self._driver_parameters
            return None

    def _advance_candidate_or_raise(self, detail, *, need_restart=False):
        failed_name = self._candidates[self._candidate_index]
        self._candidate_errors.append(f'{failed_name}: {detail}')
        if self.kind == 3 and self.info is None and self._candidate_index + 1 < len(self._candidates):
            self._select_candidate(self._candidate_index + 1)
            self.open_sent = False
            self._driver_parameters = None
            self._awaiting_callback_since = None
            if need_restart and self.handle:
                self._restarting_host = True
                self.host.astra_host_close(self.handle)
            return True
        # A single sticky verdict serves every consumer. Do not append the same
        # failure on each UI/PCM poll, or leave an already failed stream running.
        self._open_failure = '；'.join(self._candidate_errors)
        if self.handle:
            self.host.astra_host_close(self.handle)
        raise RuntimeError(self._open_failure)

    def poll(self):
        with self._lock:
            if self._open_failure and not self.closing:
                raise RuntimeError(self._open_failure)
            if not self.handle:
                return None
            s = record(HostSnapshot)
            if not self.host.astra_host_snapshot(self.handle, C.byref(s)):
                raise RuntimeError('音频设备状态不可用。')
            self._last_host_snapshot = s
            # Bound *unchanged native phases*, never an item's cumulative time.
            # ASIO release must be acknowledged, however long a vendor call takes.
            if self.kind == 3 and not self.info and not self.closing:
                phase = ('release' if self._restarting_host and not s.released else
                         'init' if s.state == 0 else
                         'open' if s.state == 2 or (s.state == 1 and self.open_sent and not s.error) else None)
                key = (self._candidate_index, phase)
                now = monotonic()
                if key != self._open_stage:
                    self._open_stage, self._open_stage_started = key, now
                elif phase and now - self._open_stage_started >= 60:
                    if phase == 'release':
                        self._open_failure = '上一个 ASIO 驱动尚未确认释放；不会启动第二条输出。请检查音频输出设置。'
                        raise RuntimeError(self._open_failure)
                    stage = '初始化' if phase == 'init' else '打开'
                    if self._advance_candidate_or_raise(
                        f'ASIO {stage}持续 60 秒没有进展，已请求停止；等待驱动确认释放。', need_restart=True
                    ):
                        return None
            if self.closing:
                if not s.released:
                    self.host.astra_host_close(self.handle)
                elif self.host.astra_host_destroy(self.handle):
                    self.handle = None
                    self.mapping.kept_unmap(self.mem)
                    self.mem = None
                    self._direct_core = None
                return None
            if self._restarting_host:
                if not s.released:
                    self.host.astra_host_close(self.handle)
                    return None
                if not self.host.astra_host_destroy(self.handle):
                    return None
                self.handle = self._create_host_handle()
                self._restarting_host = False
                self._last_host_snapshot = None
                if not self.handle:
                    raise RuntimeError('无法重新创建 ASIO 宿主以尝试备用驱动。')
                return None
            if self.stat(5):
                self._driver_parameters = None
                frames = self.info['frames'] if self.info else self.stat(4)
                raise RuntimeError(f'音频回调大小改变：已确认 {frames} 帧，最后收到 {s.observed_frames} 帧。')
            msg = bytes(s.message).decode('utf8', 'replace').rstrip('\x00')
            if s.released or s.state in (6, 7):
                if self._advance_candidate_or_raise(msg or '音频流已停止。', need_restart=True):
                    return None
            if s.state == 1 and self.open_sent and s.error:
                # The ready-with-error snapshot has no operation sequence: on
                # the same handle the prior error can race the next open. Use
                # a confirmed release boundary for failed physical candidates.
                if self._advance_candidate_or_raise(msg or f'ASIO 错误 {s.error}', need_restart=True):
                    return None
            if s.state == 3 and s.error:
                raise RuntimeError(msg or '音频流已停止。')
            name = self.device['native_selector']['exact_device'].encode('utf8')
            if s.state == 1 and not self.open_sent:
                if not self.host.astra_host_open_backend(self.handle, self.kind, name, self.device['sample_rate'], self.device['buffer_frames'] or 0):
                    raise RuntimeError('音频设备拒绝打开。')
                self.open_sent = True
            if (self.kind == 3 and s.state in (2, 3) and s.sample_rate == self.device['sample_rate']
                    and s.device == name and self._driver_parameters is None):
                route = record(HostRoute)
                if self.host.astra_host_route(self.handle, C.byref(route)):
                    self._driver_parameters = asio_open_parameters(self.host, self.handle, s, route, name.decode())
            if s.state == 3 and self.info is None and not (s.observed_frames and self.stat(4)):
                # Pa_StartStream returned successfully. A driver that supplies
                # no callback is not a usable route, unlike one still opening.
                from time import perf_counter
                now = perf_counter()
                if self._awaiting_callback_since is None:
                    self._awaiting_callback_since = now
                elif now - self._awaiting_callback_since >= 5:
                    detail = ('ASIO 已启动但 5 秒内未收到首个音频回调。' if not s.callbacks
                              else 'ASIO 已收到回调但 5 秒内未得到有效的缓冲帧数。')
                    if self._advance_candidate_or_raise(detail, need_restart=True):
                        return None
            if s.state == 3 and s.observed_frames and self.stat(4) and self.info is None:
                r = record(HostRoute)
                if (not self.host.astra_host_route(self.handle, C.byref(r)) or r.host_type != self.kind
                        or r.device_index < 0 or r.raw or (self.kind == 13 and r.shared != 1)
                        or s.device != name or s.sample_rate != self.device['sample_rate']
                        or s.observed_frames != self.stat(4) or s.observed_frames > 16384):
                    raise RuntimeError('实际输出与所选设备或缓冲不一致。')
                self._last_route_dict = _route_to_dict(self.host, self.handle, r)
                self.info = dict(name=self.name, device=name.decode(), kind=self.kind,
                                 index=r.device_index, shared=r.shared, rate=s.sample_rate,
                                 frames=s.observed_frames, latency=s.output_latency)
            return self.info

    def lease(self, route):
        with self._lock:
            info = self.poll()
            if info is None:
                return False
            if 'kept_endpoint' not in route.device:
                self.generation += 1
                selected = self.stat(0)
                token = (self.generation << 1) | (1 - (selected & 1))
                # Sync route.device identity with the actual verified open ASIO driver.
                route.device['device_id'] = self.device['device_id']
                route.device['native_selector'] = dict(self.device['native_selector'])
                route.device['kept_endpoint'] = dict(info, token=token)
                if not self.stat(0):
                    self.request(token)
            return True

    def close(self):
        with self._lock:
            if self.mem:
                self.mapping.kept_direct_mode(self.mem, 0)
            if self.handle and not self.closing:
                self.request(0)
                self.closing = True
                self.host.astra_host_close(self.handle)


class KeptDirectHost:
    """NativeDeviceHost-compatible adapter attaching a Tracks PCM core to the shared KeptEndpoint.

    Never opens or closes the underlying physical ASIO stream itself; it only attaches
    and detaches the wait-free in-process direct PCM lane on KeptEndpoint.
    """
    def __init__(self, endpoint, core):
        self.endpoint = endpoint
        self.core = core
        self._opened = False
        self._attached = False
        self._closed = False

    def poll(self):
        if self._closed:
            return {'released': True, 'state': 5, 'error': 0, 'message': ''}
        info = self.endpoint.poll()
        snap = self.endpoint.snapshot_dict()
        if self.endpoint.closing or not self.endpoint.handle:
            raise RuntimeError('共享音频端点已停止或正在关闭。')
        # A retiring PortAudio candidate can report released while the shared
        # endpoint is still progressing an approved fallback on this lease.
        # Terminal endpoint errors are raised by endpoint.poll(), not by this
        # transient host sample.
        snap['released'] = False
        if not self._opened:
            snap['state'] = 1
            snap['error'] = 0
            return snap
        if info is not None and self._attached:
            snap['state'] = 3
            snap['error'] = 0
            return snap
        snap['state'] = 2
        snap['error'] = 0
        return snap

    def open(self, driver=None, rate=None, block=0, backend='asio'):
        if self._closed:
            raise RuntimeError('host closed')
        if rate is not None and abs(rate - self.endpoint.device['sample_rate']) > 0.01:
            raise ValueError('device rate must match shared ASIO stream rate')
        self.endpoint.attach_direct(self.core)
        self._attached = True
        self._opened = True
        return True

    def route(self):
        return self.endpoint.route_dict()

    def asio_parameters(self, sample, route, expected):
        return self.endpoint.driver_parameters()

    def close(self):
        self._closed = True
        if self._attached:
            if self.endpoint.detach_direct(self.core):
                self._attached = False
                self.core = None

    def finish_close(self):
        if not self._attached:
            self.core = None
            return True
        if self.endpoint.detach_direct(self.core):
            self._attached = False
            self.core = None
            return True
        return False


class ProducerHost:
    """Implements the bridge's host lease protocol using verified endpoint metadata."""
    def __init__(self, info, snapshot, capacity):
        self.info=info
        self.lib=mapping_library()
        self.snapshot=snapshot
        self.capacity=capacity
        self.mem=self.producer=None
        self.closing=False

    def astra_host_create_owned(self, _path, callback, user, retain, release):
        self.mem=self.lib.kept_map(self.info['name'],0)
        if not self.mem:return None
        self.producer=self.lib.kept_producer_v2(self.mem,self.info['token'],callback,user,retain,release,
                                              C.cast(self.snapshot,P),self.capacity)
        if not self.producer:
            self.lib.kept_unmap(self.mem)
            self.mem=None
        return self.producer

    def astra_host_snapshot(self, handle, target):
        s=C.cast(target,C.POINTER(HostSnapshot)).contents
        state=self.lib.kept_producer_state(handle)
        s.state=5 if self.closing else 6 if state==2 else 3
        s.released=bool(self.closing and state)
        s.observed_frames=self.info['frames']
        s.sample_rate=self.info['rate']
        s.output_latency=self.info['latency']
        s.device=self.info['device'].encode()
        if state==2:s.message=b'Kept endpoint producer halted'
        return 1

    def astra_host_route(self, _handle, target):
        r=C.cast(target,C.POINTER(HostRoute)).contents
        r.host_type,r.device_index,r.shared,r.raw=self.info['kind'],self.info['index'],self.info['shared'],0
        return 1

    def astra_host_close(self, handle):
        self.closing=True
        self.lib.kept_stop(handle)
        return 1

    def astra_host_destroy(self, handle):
        if not self.lib.kept_destroy(handle):return 0
        self.lib.kept_unmap(self.mem)
        self.producer=self.mem=None
        return 1
