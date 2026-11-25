"""Control-only ctypes wrapper for the native fixed-budget PCM stream cache.

No Python function is registered as an audio callback/provider. read_address and
request_address point directly into the C++ DLL. Keep this object strongly owned
by the core/session until the native provider is released. Shutdown order:
host close+confirmed release -> core close -> cache close+finish_close.
Creation runs on a non-realtime worker; read_for_test is diagnostic only.
"""
import ctypes as C
import threading
from pathlib import Path
import numpy as np
from .native_assets import native_library
from .native_core import NativeTransportCore, CoreSnapshot

class CacheInfo(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32),('state',C.c_uint32),
              ('sample_rate',C.c_uint32),('source_rate',C.c_uint32),('channels',C.c_uint32),
              ('block_frames',C.c_uint32),('slots',C.c_uint32),('frames',C.c_uint64),
              ('allocated_bytes',C.c_uint64),('decoded_blocks',C.c_uint64),
              ('misses',C.c_uint64),('discarded',C.c_uint64),('error',C.c_char*256)]
    @classmethod
    def empty(cls):
        value=cls();value.size=C.sizeof(cls);value.version=1;return value

class NativeStreamCache:
    def __init__(self,library=None):
        self._lock=threading.RLock();self.handle=None;self._closing=False
        path=library or native_library('astra_stream_cache.dll')
        L=self.lib=C.CDLL(str(Path(path).resolve()))
        L.astra_cache_create.argtypes=[C.c_wchar_p,C.c_wchar_p,C.c_wchar_p,C.c_uint32,C.c_uint64,C.POINTER(CacheInfo)];L.astra_cache_create.restype=C.c_void_p
        L.astra_cache_info.argtypes=[C.c_void_p,C.POINTER(CacheInfo)];L.astra_cache_info.restype=C.c_int
        L.astra_cache_request.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.c_uint64];L.astra_cache_request.restype=C.c_int
        L.astra_cache_read.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.c_uint32,C.POINTER(C.c_float)];L.astra_cache_read.restype=C.c_int
        for name in ('close','retain','release'):
            fn=getattr(L,'astra_cache_'+name);fn.argtypes=[C.c_void_p];fn.restype=None
        self.sample_rate=None;self.frames=None;self._last_info=None
    def load(self,path,sndfile_dll,samplerate_dll,rate=48000,budget_bytes=8*1024*1024):
        if not isinstance(rate,int) or not 8000<=rate<=384000:raise ValueError('invalid sample rate')
        if not isinstance(budget_bytes,int) or not 0<budget_bytes<2**64:raise ValueError('invalid budget')
        with self._lock:
            if self.handle:raise RuntimeError('cache already loaded')
            value=CacheInfo.empty()
            handle=self.lib.astra_cache_create(str(Path(sndfile_dll).resolve()),str(Path(samplerate_dll).resolve()),str(Path(path).resolve()),rate,budget_bytes,C.byref(value))
            if not handle:raise RuntimeError('native cache load failed: '+value.error.decode('utf8',errors='replace'))
            self.handle=handle;self._closing=False;self.sample_rate=value.sample_rate;self.frames=value.frames
            self._last_info=self._to_dict(value)
            return self
    @staticmethod
    def _to_dict(value):
        return {name:(getattr(value,name).decode('utf8',errors='replace') if isinstance(getattr(value,name),bytes) else getattr(value,name)) for name,_ in value._fields_}
    def info(self):
        with self._lock:
            if not self.handle:raise RuntimeError('cache not loaded or released')
            value=CacheInfo.empty()
            if not self.lib.astra_cache_info(self.handle,C.byref(value)):raise RuntimeError('cache ABI mismatch')
            self._last_info=self._to_dict(value);return dict(self._last_info)
    def request(self,frame,generation=0,source=0):
        if not all(isinstance(x,int) and 0<=x<2**64 for x in (frame,generation)):raise ValueError('invalid frame/generation')
        if source!=0:raise ValueError('only source 0 is supported by one cache')
        with self._lock:
            if not self.handle or self._closing:raise RuntimeError('cache closed')
            return bool(self.lib.astra_cache_request(self.handle,source,frame,generation))
    def read_for_test(self,frame,count=256,source=0):
        if not isinstance(frame,int) or not 0<=frame<2**64:raise ValueError('invalid frame')
        if not isinstance(count,int) or not 0<count<=16384:raise ValueError('invalid count')
        if source!=0:raise ValueError('only source 0 is supported')
        with self._lock:
            if not self.handle:raise RuntimeError('cache released')
            out=np.empty((count,2),np.float32)
            result=self.lib.astra_cache_read(self.handle,source,frame,count,out.ctypes.data_as(C.POINTER(C.c_float)))
            return result,out
    def _address(self,name):return C.cast(getattr(self.lib,'astra_cache_'+name),C.c_void_p).value
    @property
    def read_address(self):return self._address('read')
    @property
    def request_address(self):return self._address('request')
    @property
    def retain_address(self):return self._address('retain')
    @property
    def release_address(self):return self._address('release')
    def close(self):
        """Request shutdown, without waiting. Never called from an audio callback."""
        with self._lock:
            if self.handle and not self._closing:
                self._closing=True;self.lib.astra_cache_close(self.handle)
    def finish_close(self):
        """Release base ownership only after the worker is stopped. Does not unload DLL."""
        with self._lock:
            if not self.handle:return True
            if not self._closing:return False
            if self.info()['state']==1:return False
            self.lib.astra_cache_release(self.handle);self.handle=None;return True


class NativeStreamCore(NativeTransportCore):
    """Provider-backed core, no full-PCM temporary or Python callback trampoline.

    The native core retains the provider handle. This strong Python reference also
    keeps the provider DLL loaded until every host's strong core reference ends.
    Do not call provider.close while the core is still intended to play.
    """
    def __init__(self,provider,library=None,*,frames=None):
        self._lock=threading.RLock();self.handle=None;self.provider=provider
        self.lib=C.CDLL(str(library or native_library('astra_audio_core.dll')))
        L=self.lib
        L.astra_core_abi_version.restype=C.c_uint32
        if L.astra_core_abi_version()!=2:raise RuntimeError('incompatible native audio ABI')
        L.astra_core_create_provider.argtypes=[C.c_void_p]*5+[C.c_uint64,C.c_double];L.astra_core_create_provider.restype=C.c_void_p
        L.astra_core_submit.argtypes=[C.c_void_p,C.c_int,C.c_double,C.c_double];L.astra_core_submit.restype=C.c_int64
        L.astra_core_snapshot.argtypes=[C.c_void_p,C.POINTER(CoreSnapshot)];L.astra_core_snapshot.restype=C.c_int
        L.astra_core_destroy.argtypes=[C.c_void_p];L.astra_core_destroy.restype=None
        L.astra_core_process.argtypes=[C.c_void_p,C.POINTER(C.c_float),C.c_uint];L.astra_core_process.restype=None
        L.astra_core_position.argtypes=[C.c_void_p];L.astra_core_position.restype=C.c_uint64
        L.astra_core_provider_misses.argtypes=[C.c_void_p];L.astra_core_provider_misses.restype=C.c_uint64
        L.astra_core_provider_error.argtypes=[C.c_void_p];L.astra_core_provider_error.restype=C.c_int
        with provider._lock:
            if not provider.handle or provider._closing:raise RuntimeError('provider closed')
            self.sample_rate=provider.sample_rate
            if frames is not None and (type(frames) is not int or not provider.frames<=frames<2**64):raise ValueError('invalid provider timeline frames')
            self.frames=provider.frames if frames is None else frames
            args=(provider.handle,provider.read_address,provider.request_address,provider.retain_address,provider.release_address,self.frames,self.sample_rate)
            create_hint=getattr(L,'astra_core_create_provider_hint',None)
            hint=getattr(provider.lib,'astra_cache_hint',None) if not hasattr(provider,'hint_address') else provider.hint_function
            self.loop_hint_enabled=bool(create_hint and hint)
            self.handle=self._create_native(args,create_hint,hint)
            if not self.handle:raise RuntimeError('native provider core creation failed')
    def _create_native(self,args,create_hint,hint):
        if self.loop_hint_enabled:
            create_hint.argtypes=[C.c_void_p]*5+[C.c_uint64,C.c_double,C.c_void_p];create_hint.restype=C.c_void_p
            return create_hint(*args,C.cast(hint,C.c_void_p).value)
        return self.lib.astra_core_create_provider(*args)
    def provider_status(self):
        with self._lock:
            if not self.handle:raise RuntimeError('core closed')
            return dict(misses=self.lib.astra_core_provider_misses(self.handle),error=self.lib.astra_core_provider_error(self.handle))


class AuditionProvider(C.Structure):
    _fields_ = [(name, C.c_void_p) for name in
                ('handle', 'read', 'request', 'hint', 'prefetch', 'retain', 'release', 'ready')]


class AuditionSnapshot(C.Structure):
    _fields_ = [('clock', CoreSnapshot), ('heard_transition', C.c_uint64),
                ('output_overrange', C.c_uint64), ('heard_source', C.c_uint32),
                ('pre_headroom_peak', C.c_float)]


class WebControls(C.Structure):
    _fields_=[(name,C.c_uint64) for name in ('serial','command','seek','ingress_ns')]
    _fields_ += [('target',C.c_double),('source',C.c_uint32),('playing',C.c_uint32)]


class NativeAuditionCore(NativeStreamCore):
    """One callback and DAC clock for the original and all aligned stems."""

    def __init__(self, providers, source, *, frames):
        self.providers = tuple(providers)
        self.initial_source = source
        self.web_route = 0
        super().__init__(providers[source], frames=frames)

    def bind_web_route(self):
        with self._lock:
            bind = self.lib.astra_core_bind_web_route
            bind.argtypes = (C.c_void_p,)
            bind.restype = C.c_int
            self.web_route = bind(self.handle)
            if not self.web_route:
                raise RuntimeError('Native WebChannel route could not bind')
            return self.web_route

    def unbind_web_route(self):
        with self._lock:
            if self.web_route:
                unbind = self.lib.astra_core_unbind_web_route
                unbind.argtypes = (C.c_int,)
                unbind.restype = None
                unbind(self.web_route)
                self.web_route = 0

    def web_controls(self):
        with self._lock:
            read = self.lib.astra_core_web_intent
            read.argtypes = (C.c_void_p,C.POINTER(WebControls),C.c_uint32)
            read.restype = C.c_int
            out = WebControls()
            if not self.handle or not read(self.handle,C.byref(out),C.sizeof(out)):
                return None
            return out

    def close(self):
        self.unbind_web_route()
        return super().close()

    def _create_native(self, args, create_hint, hint):
        inputs = (AuditionProvider * len(self.providers))(*(
            AuditionProvider(cache.handle, cache.read_address, cache.request_address,
                             cache._address('hint'), cache._address('prefetch'),
                             cache.retain_address, cache.release_address, cache._address('ready'))
            for cache in self.providers
        ))
        create = self.lib.astra_core_create_audition
        create.argtypes = [C.POINTER(AuditionProvider), C.c_uint32, C.c_uint32,
                           C.c_uint64, C.c_double]
        create.restype = C.c_void_p
        snapshot = self.lib.astra_core_audition_snapshot
        snapshot.argtypes = [C.c_void_p, C.POINTER(AuditionSnapshot), C.c_uint32]
        snapshot.restype = C.c_int
        return create(inputs, len(inputs), self.initial_source, self.frames, self.sample_rate)

    def snapshot(self):
        with self._lock:
            if not self.handle:
                raise RuntimeError('core closed')
            sample = AuditionSnapshot()
            sample.clock.size = C.sizeof(CoreSnapshot)
            if self.lib.astra_core_audition_snapshot(self.handle, C.byref(sample), C.sizeof(sample)) != 0:
                raise RuntimeError('native audition snapshot ABI mismatch')
            result = {key: getattr(sample.clock, key) for key, _ in CoreSnapshot._fields_}
            for key, _ in AuditionSnapshot._fields_[1:]:
                result[key] = getattr(sample, key)
            result['audible_position'] = sample.clock.audible_source_frame / self.sample_rate
            result['rendered_position'] = sample.clock.rendered_source_frame / self.sample_rate
            return result
