"""Worker-thread control of one owned native VST callback lane (value API v1).

A RealtimeLane and its Plugin must remain owned until close_ready() is true.
There is deliberately no destructor, Python callback, wait loop or device fallback.
"""
from __future__ import annotations

from collections import deque
import ctypes as C
from itertools import count
import math
from pathlib import Path
import sys
import threading
import time

from ..audio_output.native_host import HostRoute, HostSnapshot


class RealtimeError(RuntimeError):
    pass


_U, _I, _Q, _S, _D, _B, _P = (C.c_uint32, C.c_int32, C.c_uint64, C.c_int64,
                            C.c_double, C.c_uint8, C.c_void_p)
_MAX_SAMPLE = (1 << 63) - 1
_MAX_ID = (1 << 64) - 1
_VERSION = 3
# Contract shared with product validation and native valid_mix (live.cpp).
VST_MIDI_GAIN_MAX = 10. ** (6. / 20.)
VST_RATE_UNIT = 10000  # ASTRA_LIVE_RATE_ONE; unchanged hardware sample rate
_IDS = count(1)  # never recycled when a facade is replaced in the worker
_BUSY, _STALE = -3, -9
_BACKENDS = {'asio': 3, 'wasapi-shared': 13}
_OPS = {'play': 0, 'pause': 1, 'seek': 2, 'stop': 3, 'loop_on': 4, 'loop_off': 5,
        'on': 6, 'off': 7, 'clear': 8, 'rate': 10, 'count_play': 11, 'score_rate': 12}
_RESULTS = (('applied', None), ('stale', 'obsolete'), ('failed', 'range'),
            ('failed', 'no_timeline'), ('failed', 'audition_capacity'), ('failed', 'processor_fault'))
_FAULTS = ('', 'invalid callback buffer/block size', 'MIDI capacity', 'voice or note-ID capacity',
           'callback work budget', 'VST process failed', 'VST park cleanup failed')


def _fields(*groups):
    return [(name, type_) for type_, names in groups for name in names.split()]


_HEADER = (_U, 'size version')


class _Config(C.Structure):
    _fields_ = _fields(_HEADER, (_U, 'max_frames reserved'), (_D, 'sample_rate gain'), (_U, 'muted reserved2'), (_D, 'midi_gain audio_gain click_gain'), (_U, 'metronome bus_mode'))


class _Spec(C.Structure):
    _fields_ = _fields(_HEADER, (_U, 'event_count clock_count'), (_Q, 'id epoch sequence'),
                      (_S, 'duration'), (_D, 'sample_rate'))


class _Event(C.Structure):
    _fields_ = _fields((_S, 'sample'), (_B, 'status data1 data2 reserved'), (_U, 'reserved2'))


class _Clock(C.Structure):
    _fields_ = _fields((_S, 'sample'), (_D, 'beat bpm bar_start_beat'), (_I, 'numerator denominator'))


class _PcmSource(C.Structure):
    _fields_ = _fields(_HEADER, (_D, 'sample_rate'), (_Q, 'frames'), (_P, 'handle request read hint'))


class _Click(C.Structure):
    _fields_ = _fields((_S, 'sample'), (_U, 'accent reserved'))


class _Command(C.Structure):
    _fields_ = _fields(_HEADER, (_U, 'op reserved'), (_Q, 'id epoch sequence intent token'),
                      (_S, 'a b'), (_U, 'channel pitch velocity reserved2'), (_D, 'gain'), (_U, 'muted reserved3'), (_D, 'midi_gain audio_gain click_gain'), (_U, 'metronome bus_mode'))


class _Receipt(C.Structure):
    _fields_ = _fields(_HEADER, (_U, 'result reserved'), (_Q, 'id'))


class _View(C.Structure):
    _fields_ = _fields(_HEADER, (_U, 'state loop has_timeline fault'), (_I, 'processor_code'),
                      (_U, 'parked'), (_Q, 'epoch sequence last_intent callbacks output_frames underruns process_calls'),
                      (_S, 'position'), (_D, 'gain effective_gain'), (_U, 'muted position_fraction'), (_D, 'midi_gain audio_gain click_gain'), (_U, 'metronome rate_basis'))


def _record(cls, version=_VERSION, **values):
    return cls(size=C.sizeof(cls), version=version, **values)


def _bind(lib, prefix, signatures):
    for name, result, args in signatures:
        f = getattr(lib, prefix + name)
        f.restype, f.argtypes = result, args


def _text(value):
    return value.decode('utf-8', 'replace') if value else ''


def _load(path):
    return C.CDLL(str(Path(path).resolve()))


def _object(value, name):
    if not isinstance(value, dict):
        raise RealtimeError(f'{name} must be a JSON object')
    return value


def _str(value, name, limit=512):
    if not isinstance(value, str) or not value or len(value) > limit or '\0' in value:
        raise RealtimeError(f'Invalid {name}')
    return value


def _int(value, name, low=0, high=_MAX_ID):
    if type(value) is not int or not low <= value <= high:
        raise RealtimeError(f'Invalid {name}: expected integer in [{low}, {high}]')
    return value


def _num(value, name, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise RealtimeError(f'Invalid {name}')
    return float(value)


def _mix_values(value):
    keys = ('master_gain', 'midi_gain', 'audio_gain', 'click_gain')
    if any(k in value for k in keys):
        if 'gain' in value:
            raise RealtimeError('Do not combine legacy gain and explicit bus gains')
        master = _num(value.get('master_gain'), 'master_gain', 0., 1.)
        midi = _num(value.get('midi_gain'), 'midi_gain', 0., VST_MIDI_GAIN_MAX)
        audio = _num(value.get('audio_gain', 1.), 'audio_gain', 0., 1.)
        click = _num(value.get('click_gain', 1.), 'click_gain', 0., 1.)
    else:
        master = _num(value.get('gain'), 'gain', 0., 1.)
        midi = audio = click = 1.
    muted, metronome = value.get('muted'), value.get('metronome', False)
    if type(muted) is not bool or type(metronome) is not bool:
        raise RealtimeError('muted and metronome must be boolean')
    return dict(gain=master, midi_gain=midi, audio_gain=audio, click_gain=click,
                muted=int(muted), metronome=int(metronome), bus_mode=1)


class RealtimeLane:
    _journal_limit = 1024

    def __init__(self, plugin, *, bridge_library, device_library, portaudio_library):
        self._thread = threading.get_ident()
        self._scoped_owner = None
        self._plugin = plugin
        try:
            plugin._check()
            self._vst = plugin.host.dll
            self._rt = _load(bridge_library)
            self._host = _load(device_library)
            self._pa_path = str(Path(portaudio_library).resolve())
            ptr = C.POINTER
            _bind(self._rt, 'astra_rt_', [
                ('abi_version', _U, []), ('live_version', _U, []), ('live_error', C.c_char_p, []),
                ('live_create', _I, [ptr(_Config), _P, _P, ptr(_P)]),
                ('live_publish', _I, [_P, ptr(_Spec), ptr(_Event), ptr(_Clock), ptr(_Q), ptr(_U)]),
            ('live_publish_clicks', _I, [_P, ptr(_Spec), ptr(_Event), ptr(_Clock), ptr(_Click), _U, ptr(_Q), ptr(_U)]),
                ('live_bind_pcm', _I, [_P, _Q, ptr(_PcmSource)]),
            ('live_submit', _I, [_P, ptr(_Command)]), ('live_snapshot', _I, [_P, ptr(_View)]),
                ('live_poll', _I, [_P, ptr(_Receipt)]), ('destroy', _I, [_P]),
                ('get_callback', _P, []), ('device_retain', None, [_P]), ('device_release', None, [_P]),
                ('request_park', _I, [_P, ptr(_Q)]), ('park_status', _I, [_P, _Q, ptr(_U)]),
                ('resume', _I, [_P, _Q]),
            ])
            _bind(self._host, 'astra_host_', [
                ('create_owned', _P, [C.c_wchar_p, _P, _P, _P, _P]),
                ('open_backend', _I, [_P, _I, C.c_char_p, _D, _U]),
                ('snapshot', _I, [_P, ptr(HostSnapshot)]), ('route', _I, [_P, ptr(HostRoute)]),
                ('close', _I, [_P]), ('destroy', _I, [_P]),
            ])
            _bind(self._vst, 'astra_vst_', [
                ('rt_version', _U, []), ('rt_configuration', _I, [_P, ptr(_D), ptr(_U)]),
                ('rt_rearm', _I, [_P]), ('state_generation', _Q, [_P]),
            ])
            if (self._rt.astra_rt_abi_version(), self._rt.astra_rt_live_version(),
                    self._vst.astra_vst_rt_version()) != (1, _VERSION, 2):
                raise RealtimeError('VST realtime native ABI mismatch')
            self._process = C.cast(self._vst.astra_vst_process_rt, _P)
            rate, block = _D(), _U()
            if self._vst.astra_vst_rt_configuration(plugin.handle, C.byref(rate), C.byref(block)):
                raise RealtimeError(self._plugin_error('Plugin processing setup unavailable'))
        except RealtimeError:
            raise
        except Exception as exc:
            raise RealtimeError(f'Cannot initialize VST realtime libraries: {exc}') from exc
        self._rate, self._max_block = rate.value, block.value
        self._latency = int(self._vst.astra_vst_latency(plugin.handle))
        self._bridge = self._device = None
        self._phase = 'closed'
        self._last_error = None
        self._reopen = False
        self._ticket = 0
        self._epoch = self._project = self._sequence = None
        self._epoch_number = 0
        self._epochs = {}
        self._intent = 0
        self._tokens = {}
        self._waiting = {}
        self._journal = deque()
        self._receipt_serial = self._reported = 0
        self._view = _record(_View)
        self._route = self._actual = None
        self._output_latency = None
        self._open_sent = self._close_sent = self._closing = False

    def _check(self):
        if threading.get_ident() != self._thread:
            raise RealtimeError('RealtimeLane requires its creating worker UI thread')

    def _plugin_error(self, default):
        return _text(self._vst.astra_vst_last_error()) or default

    def _native(self, rc, operation):
        if rc:
            raise RealtimeError(f'{operation}: native bridge error {rc}')

    def _complete(self, id_, result, reason=None):
        meta = self._waiting.pop(id_, None)
        if meta is None:
            raise RealtimeError(f'Unexpected realtime receipt {id_}')
        token = meta.pop('_token', None)
        if token is not None and result != 'applied':
            name, value = token
            if self._tokens.get(name) == (value, id_):
                del self._tokens[name]
        out = dict(meta, command_id=id_, status=result)
        if reason is not None:
            out['reason'] = reason
        self._receipt_serial += 1
        self._journal.append((self._receipt_serial, out))

    def _drain(self):
        # No new submissions on this thread during the drain, so it is finite.
        if self._bridge:
            r = _record(_Receipt)
            while True:
                rc = self._rt.astra_rt_live_poll(self._bridge, C.byref(r))
                if rc == 1:
                    break
                self._native(rc, 'Poll receipt')
                result, reason = _RESULTS[r.result]
                self._complete(r.id, result, reason)

    def _space(self):
        while len(self._waiting) + len(self._journal) >= self._journal_limit:
            if not self._journal or self._journal[0][0] > self._reported:
                return False
            self._journal.popleft()
        return True

    def _view_native(self):
        self._drain()
        if self._bridge:
            s = _record(_View)
            self._native(self._rt.astra_rt_live_snapshot(self._bridge, C.byref(s)), 'Live snapshot')
            self._view = s
            self._view_sampled_at = time.perf_counter()

    def _send_close(self):
        if self._device and not self._close_sent:
            if self._host.astra_host_close(self._device):
                self._close_sent = True
            else:
                self._last_error = 'Device owner rejected retirement; release is not established'

    def _fail(self, error):
        self._last_error = self._last_error or error
        if not self._closing:
            self._phase = 'error'
        self._send_close()

    def _finish(self):
        if self._device or not self._bridge:
            return
        # The previous host_destroy has joined the owner, including the last
        # release hook. Create failure is also safe under synchronous retain.
        self._view_native()
        rc = self._rt.astra_rt_destroy(self._bridge)
        if rc == _BUSY:
            self._last_error = 'Native bridge still has a device lease'
            return
        self._native(rc, 'Retire bridge')
        self._bridge = None
        self._ticket = 0
        for id_ in tuple(self._waiting):
            self._complete(id_, 'failed', 'lane_retired')
        self._tokens.clear()
        # Only plugin control on the creating thread here, not a fake render,
        # and only after both forms of native ownership have been released.
        if self._view.process_calls:
            if self._vst.astra_vst_rt_rearm(self._plugin.handle):
                self._reopen = True
                self._last_error = self._plugin_error('Plugin requires reopen after device retirement')
            else:
                self._latency = int(self._vst.astra_vst_latency(self._plugin.handle))
        self._phase = 'error' if self._last_error else 'closed'
        self._actual = None
        self._output_latency = None

    def _update(self):
        self._view_native()
        if self._device:
            h = _record(HostSnapshot, 1)
            if not self._host.astra_host_snapshot(self._device, C.byref(h)):
                self._fail('Device snapshot ABI failure')
                return
            if h.state in (6, 7) or (self._open_sent and h.state == 1 and h.error):
                if h.state == 7:
                    self._last_error = _text(h.message) or 'Driver did not prove retirement'
                self._fail(_text(h.message) or f'Native device error {h.error}')
            if self._view.fault:
                f = self._view.fault
                why = _FAULTS[f] if f < len(_FAULTS) else f'native fault {f}'
                self._fail(f'{why} (processor code {self._view.processor_code})')
            if h.released:
                if not self._closing and not self._last_error:
                    self._fail('Device retired unexpectedly')
                if self._host.astra_host_destroy(self._device):
                    self._device = None
            elif self._closing or self._phase == 'error':
                self._send_close()
            elif h.state == 1 and not self._open_sent:
                selected, host_type, name = self._route
                self._open_sent = bool(self._host.astra_host_open_backend(
                    self._device, host_type, name, self._rate, selected['buffer_frames']))
                if not self._open_sent:
                    self._fail('Selected native device open was rejected')
            elif h.state == 3 and h.observed_frames and self._view.output_frames:
                self._observe_device(h)
        if not self._device and self._bridge and (self._closing or self._phase == 'error'):
            self._finish()

    def _observe_device(self, h):
        selected, host_type, name = self._route
        r = _record(HostRoute, 1)
        if (not self._host.astra_host_route(self._device, C.byref(r)) or r.host_type != host_type
                or r.device_index < 0 or r.raw or (host_type == 13 and r.shared != 1) or h.device != name):
            self._fail('Opened output does not match the selected native route')
        elif h.sample_rate != self._rate or not 0 < h.observed_frames <= self._device_capacity:
            self._fail('Negotiated device rate/block is incompatible with the plugin; reopen with a matching setup')
        else:
            self._actual = dict(selected, sample_rate=h.sample_rate, requested_buffer_frames=selected['buffer_frames'], buffer_frames=int(h.observed_frames))
            latency = h.output_latency * h.sample_rate
            self._output_latency = round(latency) if math.isfinite(latency) and latency > 0 else None
            self._phase = 'ready'

    def open_device(self, route: dict) -> dict:
        self._check()
        if self._device or self._bridge:
            raise RealtimeError('Previous native device has not retired')
        if self._reopen:
            raise RealtimeError('Reopen the plugin before opening another realtime device')
        self._plugin._check()
        route = _object(route, 'route')
        mix_values = _mix_values(route)
        backend = _str(route.get('backend'), 'backend')
        if backend not in _BACKENDS:
            raise RealtimeError('Owned VST output supports only asio and wasapi-shared; no fallback')
        identity = _str(route.get('device_id'), 'device_id', 2048)
        pair = route.get('output_channels')
        if not isinstance(pair, list) or any(type(ch) is not int for ch in pair) or pair != [0, 1]:
            raise RealtimeError('Owned PortAudio host supports only the explicit stereo pair [0, 1]')
        rate = _num(route.get('sample_rate'), 'sample_rate', 8000, 384000)
        block = _int(0 if route.get('buffer_frames') is None else route['buffer_frames'], 'buffer_frames', 0, 32768)
        kept = route.get('kept_endpoint')
        self._device_capacity = _int(kept['frames'],'driver frames',1,16384) if kept else self._max_block
        if rate != self._rate or (not kept and block > self._max_block):
            raise RealtimeError('Selected device rate/block is incompatible with the plugin; reopen with a matching setup')
        selector = _object(route.get('native_selector'), 'catalog native_selector')
        try:
            name = _str(selector.get('exact_device'), 'catalog exact_device', 254).encode('utf-8')
        except UnicodeError as exc:
            raise RealtimeError('Invalid UTF-8 native device selector') from exc
        if len(name) >= 255:
            raise RealtimeError('Native device name cannot be verified without truncation')
        selected = dict(backend=backend, device_id=identity, output_channels=[0, 1], sample_rate=rate, buffer_frames=block)
        if self._epoch is not None:
            self._epochs[self._epoch] = self._epoch_number
        self._epoch = self._project = self._sequence = None
        self._intent = 0
        self._view = _record(_View)
        self._route = (selected, _BACKENDS[backend], name)
        self._actual = self._output_latency = self._last_error = None
        self._phase = 'opening'
        self._open_sent = self._close_sent = self._closing = False
        config = _record(_Config, max_frames=self._max_block, sample_rate=self._rate, **mix_values)
        b = _P()
        rc = self._rt.astra_rt_live_create(C.byref(config), self._plugin.handle, self._process, C.byref(b))
        if rc:
            self._phase = 'error'
            self._last_error = _text(self._rt.astra_rt_live_error()) or f'Create native VST bridge: {rc}'
            raise RealtimeError(self._last_error)
        self._bridge = b
        if kept:
            from ..audio_output.kept_endpoint import ProducerHost
            self._host = ProducerHost(kept, self._rt.astra_rt_render_snapshot, self._max_block)
        self._device = self._host.astra_host_create_owned(
            self._pa_path, self._rt.astra_rt_get_callback(), b,
            C.cast(self._rt.astra_rt_device_retain, _P), C.cast(self._rt.astra_rt_device_release, _P))
        if not self._device:
            self._fail('Native device owner creation rejected')
        self._update()
        return self._status()

    def _submit_check(self):
        if threading.get_ident() == self._scoped_owner:
            # Qt is blocked in an insert vendor call. The audio bridge producer
            # is exclusively borrowed, but device/ticket/teardown are not.
            self._view_native()
            if self._view.fault or self._view.parked:
                raise RealtimeError('Realtime processing is faulted or parked')
        else:
            self._check()
            self._update()
        if self._phase not in ('opening', 'ready') or not self._bridge:
            raise RealtimeError(self._last_error or 'No open realtime lane')

    def _use_epoch(self, epoch, number):
        if epoch != self._epoch:
            self._epoch, self._epoch_number = epoch, number
            self._epochs[epoch] = number
            self._project = self._sequence = None
            self._intent = 0
            self._tokens.clear()

    def _epoch_for(self, epoch, *, publish=False):
        if epoch == self._epoch:
            return self._epoch_number
        if epoch in self._epochs or (self._epoch is not None and not publish):
            return None
        return self._epoch_number + 1

    @staticmethod
    def _reply(epoch, seq, id_=None, reason=None):
        reply = dict(accepted=id_ is not None, command_id=id_, epoch=epoch, sequence=seq)
        if reason:
            reply['reason'] = reason
        return reply

    def _score_click_gates(self):
        return getattr(self._rt,'astra_rt_live_score_click_gates',None) is not None

    def _score_count_in(self):
        return getattr(self._rt,'astra_rt_live_score_count_in',None) is not None

    def _score_rate(self):
        return getattr(self._rt,'astra_rt_live_score_rate',None) is not None

    def publish(self, timeline: dict) -> dict:
        self._submit_check()
        t = _object(timeline, 'timeline')
        if _int(t.get('version'), 'timeline version', 1, 1) != 1:
            raise RealtimeError('Unsupported timeline version')
        project = _str(t.get('project_id'), 'project_id')
        epoch = _str(t.get('epoch'), 'epoch')
        seq = _int(t.get('sequence'), 'sequence')
        num = self._epoch_for(epoch, publish=True)
        if num is None or (epoch == self._epoch and self._project is not None and self._project != project):
            return self._reply(epoch, seq, reason='stale_epoch')
        if epoch == self._epoch and self._sequence is not None and seq <= self._sequence:
            return self._reply(epoch, seq, reason='stale_sequence')
        if not self._space():
            return self._reply(epoch, seq, reason='busy')
        rate = _num(t.get('sample_rate'), 'timeline sample_rate', 8000, 384000)
        if rate != self._rate:
            raise RealtimeError('Timeline sample rate must match plugin/device setup')
        end = _int(t.get('duration_samples'), 'duration_samples', 0, _MAX_SAMPLE)
        es, cs = t.get('events'), t.get('clock')
        if not isinstance(es, list) or len(es) > 1048576 or not isinstance(cs, list) or not 1 <= len(cs) <= 262144:
            raise RealtimeError('Invalid timeline events or prepared clock size')
        events = (_Event * len(es))()
        for i, e in enumerate(es):
            e = _object(e, 'event')
            events[i] = _Event(_int(e.get('sample'), 'event sample', 0, end - 1),
                               _int(e.get('status'), 'MIDI status', 128, 255),
                               _int(e.get('data1'), 'MIDI data1', 0, 127), _int(e.get('data2'), 'MIDI data2', 0, 127))
        clock = (_Clock * len(cs))()
        for i, s in enumerate(cs):
            s = _object(s, 'clock boundary')
            if s.keys() - {'meter_valid'} != {'sample', 'beat', 'bpm', 'bar_start_beat', 'numerator', 'denominator'}:
                raise RealtimeError('Expected prepared constant-tempo clock spans; no curve interpolation is implied')
            meter_valid = s.get('meter_valid', True)
            if type(meter_valid) is not bool:
                raise RealtimeError('meter_valid must be boolean')
            clock[i] = _Clock(_int(s['sample'], 'clock sample', 0, end), _num(s['beat'], 'beat', -1e15, 1e15),
                              _num(s['bpm'], 'bpm', 0, sys.float_info.max), _num(s['bar_start_beat'], 'bar origin', -1e15, 1e15),
                              _int(s['numerator'], 'meter numerator', 1, 1024) if meter_valid else 0,
                              _int(s['denominator'], 'meter denominator', 1, 1024) if meter_valid else 0)
        id_ = next(_IDS)
        spec = _record(_Spec, id=id_, epoch=num, sequence=seq, duration=end, sample_rate=rate,
                       event_count=len(events), clock_count=len(clock))
        points = t.get('clicks', [])
        if not isinstance(points, list) or len(points) > 100000:
            raise RealtimeError('Invalid click plan size')
        clicks = (_Click * len(points))()
        for i, point in enumerate(points):
            point = _object(point, 'click')
            if type(point.get('accent')) is not bool:
                raise RealtimeError('Click accent must be boolean')
            gate=_int(point.get('rateGate',0),'click rate gate',0,0x80007530)
            if gate and (not self._score_click_gates() or not 1<=(gate&0xffff)<=30000 or gate&0x7fff0000):
                raise RealtimeError('Invalid click speed selection')
            clicks[i] = _Click(_int(point.get('sample'), 'click sample', 0, end-1), int(point['accent']), gate)
        old, unsupported = _Q(), _U()
        rc = self._rt.astra_rt_live_publish_clicks(self._bridge, C.byref(spec), events, clock, clicks, len(clicks), C.byref(old), C.byref(unsupported))
        if rc in (_BUSY, _STALE):
            return self._reply(epoch, seq, reason='busy' if rc == _BUSY else 'stale_sequence')
        if rc:
            raise RealtimeError(_text(self._rt.astra_rt_live_error()) or f'Prepare native timeline: {rc}')
        self._use_epoch(epoch, num)
        self._project, self._sequence = project, seq
        self._waiting[id_] = dict(epoch=epoch, sequence=seq, intent=None)
        if old.value:
            self._complete(old.value, 'stale', 'coalesced')
        self._view_native()
        return dict(self._reply(epoch, seq, id_), submitted_sequence=seq,
                    applied_sequence=self._applied(), unsupported_event_count=unsupported.value)

    def transport(self, command: dict) -> dict:
        self._submit_check()
        c = _object(command, 'transport')
        epoch = _str(c.get('epoch'), 'epoch')
        op = _str(c.get('op'), 'transport op')
        intent = _int(c.get('intent'), 'intent')
        seq = _int(c.get('sequence'), 'play sequence') if op == 'play' else self._sequence
        num = self._epoch_for(epoch)
        if num is None:
            return self._reply(epoch, seq, reason='stale_epoch')
        if intent < self._intent:
            return self._reply(epoch, seq, reason='stale_intent')
        if op == 'play' and self._sequence is not None and seq < self._sequence:
            return self._reply(epoch, seq, reason='stale_sequence')
        a = b = token = pulses = accent = 0
        if op == 'seek' or (op == 'play' and 'sample' in c):
            a = _int(c.get('sample'), 'sample', 0, _MAX_SAMPLE)
        elif op == 'play':
            a = -1
        elif op == 'loop':
            loop = _object(c.get('loop'), 'loop')
            enabled = loop.get('enabled')
            if type(enabled) is not bool:
                raise RealtimeError('loop.enabled must be boolean')
            op = 'loop_on' if enabled else 'loop_off'
            if enabled:
                a = _int(loop.get('start_sample'), 'loop start', 0, _MAX_SAMPLE)
                b = _int(loop.get('end_sample'), 'loop end', a + 1, _MAX_SAMPLE)
        elif op == 'rate':
            a = round(_num(c.get('rate'), 'audition rate', .25, 2.) * VST_RATE_UNIT)
        elif op == 'score_rate':
            if not self._score_rate():
                raise RealtimeError('Native extended score speed is unavailable')
            a = round(_num(c.get('rate'), 'score speed', .1, 3.) * VST_RATE_UNIT)
        elif op not in ('stop', 'pause'):
            raise RealtimeError('Unsupported realtime transport operation')
        if op=='play' and c.get('countIn') is not None:
            if not self._score_count_in():
                raise RealtimeError('Native score count-in is unavailable')
            plan=_object(c['countIn'],'count-in plan')
            b=_int(plan.get('frames'),'count-in duration',1,46_080_000)
            token=_int(plan.get('stepBasis'),'count-in step basis',10_000,153_600_000_000)
            pulses=_int(plan.get('pulses'),'count-in pulses',1,128)
            accent=_int(plan.get('accentPeriod'),'count-in accent period',1,64)
            op='count_play'
        id_ = next(_IDS)
        native = _record(_Command, id=id_, epoch=num, sequence=seq or 0, intent=intent, op=_OPS[op], a=a, b=b,
                         token=token,channel=pulses,pitch=accent)
        result = self._submit(native, epoch, seq, intent)
        if result['accepted']:
            self._intent = intent
        return result

    def audition(self, command: dict) -> dict:
        self._submit_check()
        c = _object(command, 'audition')
        epoch = _str(c.get('epoch'), 'epoch')
        op = _str(c.get('op'), 'audition op')
        if op not in ('on', 'off', 'clear'):
            raise RealtimeError('Unsupported realtime audition operation')
        num = self._epoch_for(epoch)
        if num is None:
            return self._reply(epoch, None, reason='stale_epoch')
        id_ = next(_IDS)
        native = _record(_Command, id=id_, epoch=num, op=_OPS[op])
        name = None
        if op != 'clear':
            name = _str(c.get('token'), 'audition token')
            native.token = self._tokens.get(name, (id_, 0))[0]
            if op == 'on':
                native.channel = _int(c.get('channel'), 'channel', 0, 15)
                native.pitch = _int(c.get('pitch'), 'pitch', 0, 127)
                native.velocity = _int(c.get('velocity'), 'velocity', 1, 127)
        result = self._submit(native, epoch, self._sequence, None)
        if result['accepted']:
            if op == 'clear':
                self._tokens.clear()
            elif op == 'off':
                self._tokens.pop(name, None)
            else:
                self._tokens[name] = (native.token, id_)
                self._waiting[id_]['_token'] = (name, native.token)
        return result

    def _submit(self, native, epoch, seq, intent):
        if not self._space():
            return self._reply(epoch, seq, reason='busy')
        rc = self._rt.astra_rt_live_submit(self._bridge, C.byref(native))
        if rc in (_BUSY, _STALE):
            return self._reply(epoch, seq, reason='busy' if rc == _BUSY else 'stale_epoch')
        self._native(rc, 'Submit command')
        self._use_epoch(epoch, native.epoch)
        self._waiting[native.id] = dict(epoch=epoch, sequence=seq, intent=intent)
        return self._reply(epoch, seq, native.id)

    def set_mix(self, mix: dict) -> dict:
        self._submit_check()
        mix = _object(mix, 'mix')
        mix_values = _mix_values(mix)
        if not self._space():
            return self._reply(self._epoch, self._sequence, reason='busy')
        id_ = next(_IDS)
        command = _record(_Command, op=9, id=id_, **mix_values)
        rc = self._rt.astra_rt_live_submit(self._bridge, C.byref(command))
        if rc == _BUSY:
            return self._reply(self._epoch, self._sequence, reason='busy')
        self._native(rc, 'Submit mix')
        self._waiting[id_] = dict(epoch=self._epoch, sequence=self._sequence, intent=0)
        return self._reply(self._epoch, self._sequence, id_)

    def _applied(self):
        v = self._view
        return v.sequence if self._bridge and v.has_timeline and v.epoch == self._epoch_number else None

    def _status(self, *, vendor_generation=None):
        v = self._view
        device = None if self._actual is None else dict(self._actual, output_channels=list(self._actual['output_channels']))
        ready = self._phase == 'ready' and not self._ticket and not v.parked
        render_epoch = next((name for name, number in reversed(self._epochs.items()) if number == v.epoch), None)
        state = ('stopped', 'paused', 'playing', 'counting')[v.state] if self._bridge else 'stopped'
        if v.parked and state == 'playing':
            state = 'paused'
        return dict(version=1, phase=self._phase, realtime_ready=ready, epoch=self._epoch,
                    state_generation=(int(self._vst.astra_vst_state_generation(self._plugin.handle))
                                      if vendor_generation is None else vendor_generation),
                    submitted_sequence=self._sequence, applied_sequence=self._applied(),
                    mix=dict(gain=v.gain, master_gain=v.gain, midi_gain=v.midi_gain, audio_gain=v.audio_gain, click_gain=v.click_gain, metronome=bool(v.metronome), muted=bool(v.muted), effective_gain=v.effective_gain) if v.callbacks else None,
                    transport=dict(state=state, position_samples=v.position if self._bridge else 0,
                                   position_fraction=v.position_fraction if self._bridge else 0,
                                   rate=(v.rate_basis or VST_RATE_UNIT) / VST_RATE_UNIT,
                                   position_kind='rendered_project_samples', last_intent=v.last_intent,
                                   loop_enabled=bool(self._bridge and v.loop),
                                   sampled_qpc_sec=getattr(self, '_view_sampled_at', None) if self._bridge else None),
                    render_epoch=render_epoch if self._bridge else None, device=device,
                    plugin_latency_samples=self._latency, output_latency_samples=self._output_latency,
                    underruns=v.underruns, last_error=self._last_error, control_parked=bool(v.parked and self._bridge),
                    plugin_reopen_required=self._reopen,
                    capabilities=dict(program_change=False, system_messages=False, cc='IMidiMapping',
                                      score_click_gates=self._score_click_gates(), score_count_in=self._score_count_in(),
                                      score_rate=self._score_rate()),
                    receipt_serial=self._receipt_serial, receipts=[dict(r) for _, r in self._journal])

    def status(self) -> dict:
        self._check()
        self._update()
        result = self._status()
        self._reported = self._receipt_serial
        return result

    def _scoped_prepare(self):
        self._check()
        if self._phase != 'ready' or self._ticket or not self._bridge:
            raise RealtimeError('Cannot borrow an inactive or parked native bridge')
        _bind(self._rt, 'astra_rt_live_dispatch_', [
            ('prepare', _I, [_P, C.POINTER(_Q)]),
            *((name, _I, [_P, _Q]) for name in ('claim', 'release', 'reclaim', 'cancel_unclaimed')),
        ])
        epoch = _Q()
        self._native(self._rt.astra_rt_live_dispatch_prepare(self._bridge, C.byref(epoch)), 'Borrow transport control')
        return epoch.value

    def _scoped_claim(self, epoch):
        self._native(self._rt.astra_rt_live_dispatch_claim(self._bridge, epoch), 'Claim transport control')
        self._scoped_owner = threading.get_ident()

    def _scoped_release(self, epoch):
        self._scoped_owner = None
        self._native(self._rt.astra_rt_live_dispatch_release(self._bridge, epoch), 'Release transport control')

    def _scoped_reclaim(self, epoch, *, unclaimed=False):
        self._check()
        if unclaimed:
            self._native(self._rt.astra_rt_live_dispatch_cancel_unclaimed(self._bridge, epoch), 'Cancel transport borrow')
        self._native(self._rt.astra_rt_live_dispatch_reclaim(self._bridge, epoch), 'Reclaim transport control')

    def _scoped_status(self, vendor_generation):
        if threading.get_ident() != self._scoped_owner:
            raise RealtimeError('Realtime snapshot requires current transport owner')
        self._view_native()
        result = self._status(vendor_generation=vendor_generation)
        self._reported = self._receipt_serial
        return result

    def begin_control(self) -> dict:
        self._check()
        self._update()
        if self._phase != 'ready' or not self._bridge or self._ticket:
            raise RealtimeError(self._last_error or 'Live device is not available for a new control grant')
        ticket = _Q()
        self._native(self._rt.astra_rt_request_park(self._bridge, C.byref(ticket)), 'Request control grant')
        self._ticket = ticket.value
        return {'ticket': self._ticket}

    def control_ready(self, ticket: int) -> bool:
        self._check()
        _int(ticket, 'control ticket', 1, _MAX_ID)
        self._update()
        if self._phase == 'error':
            raise RealtimeError(self._last_error or 'Native processing failed while awaiting control')
        if self._phase != 'ready' or not self._bridge or ticket != self._ticket:
            return False
        state = _U()
        self._native(self._rt.astra_rt_park_status(self._bridge, ticket, C.byref(state)), 'Check control grant')
        return state.value == 2

    def bind_pcm(self, ticket: int, source: dict | None) -> dict:
        """Trusted native-cache controller only, never expose raw pointers to IPC.

        Caller retains source/cache and its DLL until detach succeeds or the
        owned device is fully retired. All pointer targets must be native ABI.
        """
        if not self.control_ready(ticket):
            raise RealtimeError('Exact control grant has not been acknowledged')
        value = None
        if source is not None:
            source = _object(source, 'native PCM source')
            value = _record(_PcmSource, version=1,
                sample_rate=_num(source.get('sample_rate'), 'source sample_rate', 8000, 384000),
                frames=_int(source.get('frames'), 'source frames', 1, _MAX_SAMPLE),
                **{k:_int(source.get(k if k=='handle' else k+'_address'), k+' native address', 1, _MAX_ID)
                   for k in ('handle','request','read','hint')})
        self._native(self._rt.astra_rt_live_bind_pcm(self._bridge, ticket,
                     C.byref(value) if value is not None else None), 'Bind native PCM source')
        return {'bound': value is not None}

    def end_control(self, ticket: int) -> dict:
        if not self.control_ready(ticket):
            raise RealtimeError('Exact control grant has not been acknowledged')
        self._latency = int(self._vst.astra_vst_latency(self._plugin.handle))
        self._native(self._rt.astra_rt_resume(self._bridge, ticket), 'Resume native lane')
        self._ticket = 0
        return {'resumed': True}

    def close_device(self) -> dict:
        self._check()
        if self._bridge or self._device:
            self._closing = True
            self._phase = 'closing'
            self._send_close()
            self._update()
        return self._status()

    def close_ready(self) -> bool:
        self._check()
        self._update()
        return not self._device and not self._bridge
