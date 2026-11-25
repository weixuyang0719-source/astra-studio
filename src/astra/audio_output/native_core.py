"""Experimental C ABI transport adapter. Not selected by production playback.

PCM is copied into immutable native storage before use. Feed driver the native
callback address + handle, never a ctypes/Python callback trampoline. Driver must
be stopped AND joined before close; adapter alone does not manage driver ownership.
Control calls are serialized by the caller (single producer).
"""
from .native_assets import native_library
import ctypes as C
import threading
from functools import wraps

def serialized(fn):
    @wraps(fn)
    def call(self,*a,**kw):
        with self._lock:return fn(self,*a,**kw)
    return call
from pathlib import Path
import numpy as np

class CoreSnapshot(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32),('applied_commands',C.c_uint64),('rendered_frames',C.c_uint64),('rendered_source_frame',C.c_uint64),('rendered_active',C.c_uint32),('reserved',C.c_uint32),('rendered_epoch',C.c_uint64),('audible_source_frame',C.c_uint64),('audible_epoch',C.c_uint64),('audible_active',C.c_uint32),('pad',C.c_uint32),('sampled_ns',C.c_uint64)]

class NativeTransportCore:
    def __init__(self, pcm, library=None, sample_rate=48000):
        self._lock=threading.RLock()
        pcm=np.ascontiguousarray(pcm,dtype=np.float32)
        if pcm.ndim!=2 or pcm.shape[1]!=2 or not len(pcm):
            raise ValueError('expected nonempty stereo PCM at 48000Hz')
        path=library or native_library('astra_audio_core.dll')
        self.lib=C.CDLL(str(path))
        L=self.lib
        L.astra_core_abi_version.restype=C.c_uint32
        if L.astra_core_abi_version()!=2:raise RuntimeError("incompatible native audio ABI")
        L.astra_core_create.argtypes=[C.POINTER(C.c_float),C.c_uint64];L.astra_core_create.restype=C.c_void_p
        L.astra_core_create_rate.argtypes=[C.POINTER(C.c_float),C.c_uint64,C.c_double];L.astra_core_create_rate.restype=C.c_void_p
        L.astra_core_submit.argtypes=[C.c_void_p,C.c_int,C.c_double,C.c_double];L.astra_core_submit.restype=C.c_int64
        L.astra_core_audible_position.argtypes=[C.c_void_p];L.astra_core_audible_position.restype=C.c_uint64
        L.astra_core_snapshot.argtypes=[C.c_void_p,C.POINTER(CoreSnapshot)];L.astra_core_snapshot.restype=C.c_int
        L.astra_core_destroy.argtypes=[C.c_void_p];L.astra_core_destroy.restype=None
        L.astra_core_command.argtypes=[C.c_void_p,C.c_int,C.c_double,C.c_double];L.astra_core_command.restype=C.c_int
        L.astra_core_process.argtypes=[C.c_void_p,C.POINTER(C.c_float),C.c_uint];L.astra_core_process.restype=None
        L.astra_core_position.argtypes=[C.c_void_p];L.astra_core_position.restype=C.c_uint64
        L.astra_core_playing.argtypes=[C.c_void_p];L.astra_core_playing.restype=C.c_int
        self.sample_rate=sample_rate
        self.handle=L.astra_core_create_rate(pcm.ctypes.data_as(C.POINTER(C.c_float)),len(pcm),sample_rate)
        if not self.handle:raise MemoryError('native PCM allocation failed')
    @property
    def callback_address(self):return C.cast(self.lib.astra_core_pa_callback,C.c_void_p).value
    @serialized
    def command(self,kind,a=0,b=0):
        if not self.handle:raise RuntimeError('closed')
        result=self.lib.astra_core_submit(self.handle,kind,a,b)
        if result==-1:raise ValueError('invalid command')
        if result==-2:raise BufferError('native command queue full')
        return result
    @serialized
    def render_for_test(self,frames):
        if not self.handle:raise RuntimeError('closed')
        if not 0<frames<1048576:raise ValueError('invalid frames')
        out=np.empty((frames,2),np.float32)
        self.lib.astra_core_process(self.handle,out.ctypes.data_as(C.POINTER(C.c_float)),frames)
        return out
    @property
    @serialized
    def position(self):
        if not self.handle:raise RuntimeError('closed')
        return self.lib.astra_core_position(self.handle)/self.sample_rate
    @serialized
    def snapshot(self):
        if not self.handle:raise RuntimeError('closed')
        s=CoreSnapshot();s.size=C.sizeof(s);s.version=2
        if self.lib.astra_core_snapshot(self.handle,C.byref(s))!=0 or s.version!=2:raise RuntimeError('snapshot ABI mismatch')
        result={k:getattr(s,k) for k,_ in s._fields_}
        result['audible_position']=s.audible_source_frame/self.sample_rate
        result['rendered_position']=s.rendered_source_frame/self.sample_rate
        return result
    @serialized
    def close(self):
        if self.handle:self.lib.astra_core_destroy(self.handle);self.handle=None
