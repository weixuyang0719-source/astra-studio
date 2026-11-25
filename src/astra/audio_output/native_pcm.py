"""Non-real-time native decoder binding. Does not open an audio device.

Load on a background worker. Native PCM remains owned here until close; copied
into NativeTransportCore when make_core() succeeds. Budget must allow that extra
core copy as well as loader input/output buffers. No implicit DLL download.
"""
from .native_assets import native_library
import ctypes as C
from pathlib import Path
import threading
import numpy as np
from .native_core import NativeTransportCore

class PcmInfo(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32),('frames',C.c_uint64),('bytes',C.c_uint64),('sample_rate',C.c_uint32),('source_channels',C.c_uint32),('output_channels',C.c_uint32),('source_sample_rate',C.c_uint32),('error',C.c_char*256)]

class NativePcm:
    def __init__(self,library=None):
        self._lock=threading.RLock();self.handle=None
        p=library or native_library('astra_pcm_loader.dll')
        L=self.lib=C.CDLL(str(p))
        L.astra_pcm_abi_version.restype=C.c_uint32
        if L.astra_pcm_abi_version()!=1:raise RuntimeError('loader ABI mismatch')
        L.astra_pcm_load_resampled.argtypes=[C.c_wchar_p,C.c_wchar_p,C.c_wchar_p,C.c_uint32,C.c_uint64,C.c_void_p,C.POINTER(C.c_void_p),C.POINTER(PcmInfo)];L.astra_pcm_load_resampled.restype=C.c_int
        L.astra_pcm_data.argtypes=[C.c_void_p];L.astra_pcm_data.restype=C.POINTER(C.c_float)
        L.astra_pcm_release.argtypes=[C.c_void_p];L.astra_pcm_release.restype=None
    def load(self,path,sndfile_dll,samplerate_dll,rate=48000,budget_bytes=512*1024*1024):
        with self._lock:
            if self.handle:raise RuntimeError('PCM already loaded')
            info=PcmInfo();info.size=C.sizeof(info);info.version=1;handle=C.c_void_p()
            status=self.lib.astra_pcm_load_resampled(str(Path(sndfile_dll).resolve()),str(Path(samplerate_dll).resolve()),str(Path(path).resolve()),rate,budget_bytes,None,C.byref(handle),C.byref(info))
            if status:raise RuntimeError(f'native PCM load failed ({status}): '+info.error.decode('utf8',errors='replace'))
            self.handle=handle;self.info=info;self.budget=budget_bytes
            return self
    def make_core(self):
        with self._lock:
            if not self.handle:raise RuntimeError('PCM not loaded')
            if self.info.bytes*2>self.budget:raise MemoryError('loader + immutable core copy exceed session budget')
            view=np.ctypeslib.as_array(self.lib.astra_pcm_data(self.handle),shape=(self.info.frames*2,)).reshape((-1,2))
            return NativeTransportCore(view,sample_rate=self.info.sample_rate)
    def close(self):
        with self._lock:
            if self.handle:self.lib.astra_pcm_release(self.handle);self.handle=None
