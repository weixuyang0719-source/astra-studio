"""Control/offline bindings for the native VST3 host. Not a Python realtime engine.

Instantiate only in a dedicated plugin worker: a third-party DLL can crash its
process. Audio callback integrations must call the native ABI directly.
"""
from __future__ import annotations
import ctypes as C
import json
import math
import os
from time import get_clock_info
from pathlib import Path
import threading

class HostError(RuntimeError):
    pass

class MidiEvent(C.Structure):
    _fields_ = [("sample_offset", C.c_uint32), ("status", C.c_uint8), ("data1", C.c_uint8), ("data2", C.c_uint8), ("reserved", C.c_uint8)]

class InsertStatus(C.Structure):
    _fields_ = [("size", C.c_uint32), ("version", C.c_uint32),
                ("applied_revision", C.c_uint64), ("pending_revision", C.c_uint64),
                ("latency_samples", C.c_uint32), ("count", C.c_uint32)]

class NativeHost:
    def __init__(self, library):
        self.dll = C.CDLL(str(Path(library).resolve()))
        self._bind("abi_version", [], C.c_uint32)
        self._bind("last_error", [], C.c_char_p)
        self._bind("scan", [C.c_char_p, C.c_void_p, C.c_uint64], C.c_uint64)
        self._bind("open", [C.c_char_p, C.c_char_p, C.c_double, C.c_uint32], C.c_void_p)
        self._bind("process", [C.c_void_p, C.POINTER(C.c_float), C.c_uint32, C.POINTER(MidiEvent), C.c_uint32, C.c_int64, C.c_double, C.c_uint32], C.c_int32)
        self._bind("get_state", [C.c_void_p, C.c_void_p, C.c_uint64], C.c_uint64)
        self._bind("set_state", [C.c_void_p, C.c_void_p, C.c_uint64], C.c_int32)
        self._bind("latency", [C.c_void_p], C.c_uint32)
        if hasattr(self.dll, "astra_vst_state_generation"):
            self._bind("state_generation", [C.c_void_p], C.c_uint64)
        self._bind("editor_attach", [C.c_void_p, C.c_void_p, C.POINTER(C.c_int32), C.POINTER(C.c_int32)], C.c_int32)
        self._bind("editor_resize", [C.c_void_p, C.c_int32, C.c_int32], C.c_int32)
        self._bind("editor_remove", [C.c_void_p], C.c_int32)
        self._bind("close", [C.c_void_p], None)
        if hasattr(self.dll, 'astra_vst_feedback_idle_v1'):
            self._bind('feedback_idle_v1',[C.c_void_p,C.c_int],C.c_int32)
        if hasattr(self.dll, "astra_vst_open_effect"):
            self._bind("open_effect", [C.c_char_p,C.c_char_p,C.c_double,C.c_uint32], C.c_void_p)
            self._bind("set_inserts", [C.c_void_p,C.POINTER(C.c_void_p),C.POINTER(C.c_uint32),C.c_uint32,C.c_uint64], C.c_uint64)
            self._bind("inserts_status", [C.c_void_p,C.POINTER(InsertStatus)], C.c_int32)
        self.native_insert_transactions = hasattr(self.dll, "astra_vst_stage_insert_v1")
        self.isolated_inserts = hasattr(self.dll, 'astra_fx_client')
        if self.native_insert_transactions:
            for symbol, args, result in (
                ("insert_clock_ms", [], C.c_uint64),
                ("stage_insert_v1", [C.c_void_p,C.c_char_p,C.c_char_p,C.c_void_p,C.c_uint64,C.c_uint64], C.c_void_p),
                ("set_inserts_before_v1", [C.c_void_p,C.POINTER(C.c_void_p),C.POINTER(C.c_uint32),C.c_uint32,C.c_uint64,C.c_uint64], C.c_uint64),
            ):
                if not hasattr(self.dll, "astra_vst_" + symbol):
                    raise HostError("Native VST insert opt-in requires the complete C++ transaction ABI")
                self._bind(symbol,args,result)
        if self.dll.astra_vst_abi_version() != 1:
            raise HostError("VST host ABI mismatch")

    def _bind(self, name, args, result):
        f = getattr(self.dll, "astra_vst_" + name)
        f.argtypes, f.restype = args, result

    def error(self):
        text = self.dll.astra_vst_last_error()
        return HostError(text.decode("utf-8", "replace") if text else "VST host operation failed")

    def scan(self, path):
        buffer = C.create_string_buffer(4 * 1024 * 1024)
        size = self.dll.astra_vst_scan(str(Path(path).resolve()).encode("utf-8"), buffer, len(buffer))
        if not size:
            raise self.error()
        if size > len(buffer):
            raise HostError("VST metadata exceeds safety limit")
        return json.loads(buffer.value.decode("utf-8"))

    def open(self, path, class_id, sample_rate=48000, max_block=512):
        if not 8000 <= sample_rate <= 384000 or not 1 <= max_block <= 16384:
            raise ValueError("Unsupported processing setup")
        handle = self.dll.astra_vst_open(str(Path(path).resolve()).encode("utf-8"), str(class_id).upper().encode("ascii"), sample_rate, max_block)
        if not handle:
            raise self.error()
        return Plugin(self, handle, max_block)

    def open_effect(self, path, class_id, sample_rate=48000, max_block=512):
        if not hasattr(self.dll, "astra_vst_open_effect"):
            raise HostError("Native host lacks audio insert support")
        if not 8000 <= sample_rate <= 384000 or not 1 <= max_block <= 16384:
            raise ValueError("Unsupported processing setup")
        handle = self.dll.astra_vst_open_effect(str(Path(path).resolve()).encode("utf-8"),
                    str(class_id).upper().encode("ascii"), sample_rate, max_block)
        if not handle:
            raise self.error()
        return Plugin(self, handle, max_block)

    @staticmethod
    def insert_deadline_ms(deadline_ns):
        if deadline_ns is None:
            return 0
        # The parent and worker use Python 3.10's Windows GetTickCount64 clock.
        # Do not mix epochs or silently allow late work on different runtimes.
        if get_clock_info("monotonic").implementation != "GetTickCount64()":
            raise HostError("Native VST insert deadlines require the Windows GetTickCount64 monotonic clock")
        if type(deadline_ns) is not int or not 10**6 <= deadline_ns < 2**64:
            raise ValueError("Invalid native insert deadline")
        return deadline_ns // 10**6

    def stage_insert(self, instrument, path, class_id, *, state=None, deadline_ns=None):
        instrument._check()
        if self.isolated_inserts:
            from .remote_effect import RemoteEffect
            return RemoteEffect(instrument,path,class_id,instrument.host_rate,state,deadline_ns)
        if not self.native_insert_transactions or instrument.host is not self:
            raise HostError("Native insert staging is not available for this host")
        module=str(Path(path).resolve())
        class_id=str(class_id).upper()
        if state is not None and (type(state) is not bytes or not 0 < len(state) <= 64 * 1024 * 1024):
            raise ValueError("Invalid/enormous opaque plugin state")
        handle=self.dll.astra_vst_stage_insert_v1(instrument.handle,module.encode("utf-8"),
            class_id.encode("ascii"),state,len(state) if state is not None else 0,
            self.insert_deadline_ms(deadline_ns))
        if not handle:
            raise self.error()
        plugin=Plugin(self,handle,instrument.max_block)
        plugin.insert_identity=(os.path.normcase(module),class_id)
        return plugin

class Plugin:
    def __init__(self, host, handle, max_block):
        self.host, self.handle, self.max_block = host, handle, max_block
        self._thread = threading.get_ident()
        self.editor_open = False

    def _check(self):
        if not self.handle:
            raise HostError("Plugin is closed")
        if threading.get_ident() != self._thread:
            raise HostError("Plugin control/UI methods require the creating thread")

    def set_inserts(self, effects, bypass, revision, *, deadline_ns=None):
        self._check()
        if len(effects) != len(bypass) or len(effects) > 32:
            raise ValueError("Invalid insert chain size")
        if type(revision) is not int or not 0 < revision < 2**64 or any(type(v) is not bool for v in bypass):
            raise ValueError("Invalid insert revision or bypass")
        for plugin in effects:
            plugin._check()
            if plugin.host is not self.host:
                raise ValueError("Insert belongs to another host")
        pointers = (C.c_void_p * len(effects))(*(p.handle for p in effects))
        switches = (C.c_uint32 * len(bypass))(*(int(v) for v in bypass))
        if self.host.native_insert_transactions:
            result=self.host.dll.astra_vst_set_inserts_before_v1(self.handle,pointers,switches,
                len(effects),revision,self.host.insert_deadline_ms(deadline_ns))
        else:
            result=self.host.dll.astra_vst_set_inserts(self.handle,pointers,switches,len(effects),revision)
        if not result:
            raise self.host.error()
        return int(result)

    def inserts_status(self):
        self._check()
        value = InsertStatus(C.sizeof(InsertStatus), 1)
        if self.host.dll.astra_vst_inserts_status(self.handle,C.byref(value)):
            raise self.host.error()
        return {k:int(getattr(value,k)) for k in ('applied_revision','pending_revision','latency_samples','count')}

    def render_offline(self, frames, events=(), sample_position=0, tempo=120, playing=True, *, unattached=False):
        self._check()
        if not 1 <= frames <= self.max_block:
            raise ValueError("Block size exceeds configured maximum")
        if not math.isfinite(tempo) or tempo <= 0 or sample_position < 0:
            raise ValueError("Invalid transport position or tempo")
        packed = []
        previous = -1
        for offset, status, data1, data2 in events:
            if not 0 <= offset < frames or offset < previous or not 0 <= status <= 255 or not 0 <= data1 < 128 or not 0 <= data2 < 128:
                raise ValueError("Invalid or unsorted MIDI event")
            previous = offset
            packed.append(MidiEvent(offset, status, data1, data2, 0))
        midi = (MidiEvent * len(packed))(*packed)
        output = (C.c_float * (2 * frames))()
        render = self.host.dll.astra_vst_process
        args = (self.handle, output, frames, midi, len(midi), sample_position, tempo, int(playing))
        code = render(*args)
        if code == -6 and unattached and hasattr(self.host.dll, 'astra_vst_service_restart_offline_v1'):
            restart = self.host.dll.astra_vst_service_restart_offline_v1
            restart.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
            restart.restype = C.c_int32
            flags = C.c_uint32()
            if restart(self.handle, C.byref(flags)):
                raise HostError(f'VST3 restart flags 0x{flags.value:X}: {self.host.error()}')
            code = render(*args)
        if code:
            raise HostError(f'VST3 processing failed ({code})')
        return bytes(output)

    def warm_candidate_chain(self):
        self._check()
        class Context(C.Structure):
            _fields_ = [('position', C.c_int64), ('beat', C.c_double), ('bpm', C.c_double),
                        ('bar', C.c_double), ('cycle_start', C.c_double), ('cycle_end', C.c_double),
                        ('numerator', C.c_int32), ('denominator', C.c_int32),
                        ('flags', C.c_uint32), ('reserved', C.c_uint32)]
        context = Context(bpm=120, numerator=4, denominator=4, flags=2|8)
        # The ordinary offline instrument API omits inserts. Use the full chain
        # processor here so the next publication sees an applied rack revision.
        frames = min(self.max_block, 256)
        pcm = (C.c_float * (2 * frames))()
        render = self.host.dll.astra_vst_process_rt
        render.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_uint32, C.c_void_p, C.c_uint32, C.POINTER(Context)]
        render.restype = C.c_int32
        result = render(self.handle, pcm, frames, None, 0, C.byref(context))
        if result or not all(map(math.isfinite, pcm)):
            raise HostError(f'Candidate effect chain rejected its warmup block ({result})')
        return self.inserts_status()

    def state_generation(self):
        self._check()
        if not hasattr(self.host.dll, "astra_vst_state_generation"):
            raise HostError("Native host lacks state generation support")
        return int(self.host.dll.astra_vst_state_generation(self.handle))

    def get_state(self):
        self._check()
        size = self.host.dll.astra_vst_get_state(self.handle, None, 0)
        for _ in range(3):
            if not 0 < size <= 64 * 1024 * 1024:
                raise HostError("Plugin state missing or exceeds 64 MiB limit")
            buffer = C.create_string_buffer(size)
            actual = self.host.dll.astra_vst_get_state(self.handle, buffer, size)
            if not actual:
                raise self.host.error()
            if actual <= size:
                return buffer.raw[:actual]
            size = actual
        raise HostError("Plugin state size is unstable")

    def set_state(self, state, *, offline=False):
        self._check()
        if not 0 < len(state) <= 64 * 1024 * 1024:
            raise ValueError("Invalid state size")
        buffer = C.create_string_buffer(state)
        setter = self.host.dll.astra_vst_set_state
        if offline and hasattr(self.host.dll, 'astra_vst_set_state_offline'):
            setter = self.host.dll.astra_vst_set_state_offline
            setter.argtypes = [C.c_void_p, C.c_void_p, C.c_uint64]
            setter.restype = C.c_int32
        if setter(self.handle, buffer, len(state)):
            raise self.host.error()

    def attach_editor(self, hwnd):
        self._check()
        w, h = C.c_int32(), C.c_int32()
        code = self.host.dll.astra_vst_editor_attach(self.handle, hwnd, C.byref(w), C.byref(h))
        if code == 1:
            return None
        if code:
            raise self.host.error()
        self.editor_open = True
        return w.value, h.value

    def feedback_idle(self, service=False):
        self._check()
        feedback = getattr(self.host.dll, 'astra_vst_feedback_idle_v1', None)
        return feedback is not None and feedback(self.handle, int(service)) > 0

    def request_editor_size(self, width, height):
        """A completed user resize, in physical pixels. A fixed/declined view keeps its full canvas."""
        self._check()
        return self.editor_open and self.host.dll.astra_vst_editor_resize(self.handle, width, height) == 0

    def remove_editor(self):
        self._check()
        if self.editor_open and self.host.dll.astra_vst_editor_remove(self.handle):
            raise self.host.error()
        self.editor_open = False

    def reset_effect_history(self):
        self._check()
        reset=self.host.dll.astra_fx_reset_history
        reset.argtypes=[C.c_void_p]
        reset.restype=C.c_int
        if reset(self.handle):
            raise HostError("Effect cannot clear its reported processing history; a fresh instance is required")

    def close(self):
        if self.handle:
            self._check()
            self.remove_editor()
            if getattr(self,'fx_server',None):
                self.host.dll.astra_fx_stop(self.fx_server)
                self.fx_server=None
            self.host.dll.astra_vst_close(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

