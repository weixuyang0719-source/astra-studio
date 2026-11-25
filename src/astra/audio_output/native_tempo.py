"""Fixed-tempo native provider ownership; no Python realtime callbacks.
Source/core commands remain original source frames. Diagnostic provider reads use
stretched OUTPUT frames. Physical Hz and tempo ratio are distinct.
"""
import ctypes as C
import math,threading
import numpy as np
from .native_assets import native_library
from .native_stream import NativeStreamCore
class TempoInfo(C.Structure):
 _fields_=[('size',C.c_uint32),('version',C.c_uint32),('state',C.c_uint32),('sample_rate',C.c_uint32),('source_frames',C.c_uint64),('output_frames',C.c_uint64),('allocated_slots',C.c_uint64),('rendered_blocks',C.c_uint64),('misses',C.c_uint64),('resets',C.c_uint64),('tempo',C.c_double),('error',C.c_char*256)]
 @classmethod
 def empty(cls):
  x=cls();x.size=C.sizeof(cls);x.version=1;return x
class NativeTempoCache:
 def __init__(self,source,tempo,*,frames=None,library=None):
  if not math.isfinite(tempo) or not .25<=tempo<=2:raise ValueError('tempo outside .25..2')
  frames=source.frames if frames is None else frames
  if type(frames)is not int or not source.frames<=frames<2**63:raise ValueError('invalid source timeline frames')
  self._lock=threading.RLock();self.source=source;self.tempo=float(tempo);self.frames=frames;self.sample_rate=source.sample_rate;self.handle=None;self._closing=False
  self.lib=L=C.CDLL(str(library or native_library('astra_tempo_cache.dll')))
  L.astra_tempo_create.argtypes=[C.c_void_p]*6+[C.c_uint64,C.c_uint32,C.c_double,C.POINTER(TempoInfo)];L.astra_tempo_create.restype=C.c_void_p
  L.astra_tempo_info.argtypes=[C.c_void_p,C.POINTER(TempoInfo)];L.astra_tempo_info.restype=C.c_int
  for name in ('request','hint'):
   f=getattr(L,'astra_tempo_'+name);f.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.c_uint64];f.restype=C.c_int
  L.astra_tempo_read.argtypes=[C.c_void_p,C.c_uint32,C.c_uint64,C.c_uint32,C.POINTER(C.c_float)];L.astra_tempo_read.restype=C.c_int
  for name in ('retain','release','close'):
   f=getattr(L,'astra_tempo_'+name);f.argtypes=[C.c_void_p];f.restype=None
  hint=getattr(source.lib,'astra_cache_hint',None);i=TempoInfo.empty()
  with source._lock:
   if not source.handle or source._closing:raise RuntimeError('source provider closed')
   self.handle=L.astra_tempo_create(source.handle,source.read_address,source.request_address,C.cast(hint,C.c_void_p).value if hint else None,source.retain_address,source.release_address,frames,self.sample_rate,self.tempo,C.byref(i))
  if not self.handle:raise RuntimeError('native tempo create: '+i.error.decode('utf8',errors='replace'))
  self.output_frames=i.output_frames
 def _address(self,name):return C.cast(getattr(self.lib,'astra_tempo_'+name),C.c_void_p).value
 @property
 def read_address(self):return self._address('read')
 @property
 def request_address(self):return self._address('request')
 @property
 def hint_address(self):return self._address('hint')
 @property
 def hint_function(self):return self.lib.astra_tempo_hint
 @property
 def retain_address(self):return self._address('retain')
 @property
 def release_address(self):return self._address('release')
 def info(self):
  with self._lock:
   if not self.handle:raise RuntimeError('tempo provider released')
   i=TempoInfo.empty()
   if not self.lib.astra_tempo_info(self.handle,C.byref(i)):raise RuntimeError('tempo ABI mismatch')
   return {k:(getattr(i,k).decode('utf8',errors='replace') if isinstance(getattr(i,k),bytes) else getattr(i,k)) for k,_ in i._fields_}
 def request(self,output_frame,generation=0):
  if not all(type(x)is int and 0<=x<2**64 for x in (output_frame,generation)):raise ValueError('invalid output request')
  with self._lock:
   if not self.handle or self._closing:raise RuntimeError('tempo provider closed')
   return bool(self.lib.astra_tempo_request(self.handle,0,output_frame,generation))
 def read_for_test(self,output_frame,count=256):
  if type(output_frame)is not int or not 0<=output_frame<2**64 or type(count)is not int or not 0<count<=256:raise ValueError('invalid output read')
  with self._lock:
   if not self.handle:raise RuntimeError('tempo provider released')
   out=np.empty((count,2),np.float32)
   status=self.lib.astra_tempo_read(self.handle,0,output_frame,count,out.ctypes.data_as(C.POINTER(C.c_float)))
   return status,out
 def close(self):
  with self._lock:
   if self.handle and not self._closing:self._closing=True;self.lib.astra_tempo_close(self.handle)
 def finish_close(self):
  with self._lock:
   if not self.handle:return True
   if not self._closing or self.info()['state']==1:return False
   self.lib.astra_tempo_release(self.handle);self.handle=None;return True
class NativeTempoCore(NativeStreamCore):
 def _create_native(self,args,create_hint,hint):
  create=self.lib.astra_core_create_tempo_provider
  create.argtypes=[C.c_void_p]*5+[C.c_uint64,C.c_double,C.c_void_p,C.c_double];create.restype=C.c_void_p
  self.loop_hint_enabled=True
  return create(*args,self.provider.hint_address,self.provider.tempo)
