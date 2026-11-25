"""Direct native WASAPI control wrapper. Never registers a Python audio callback.
Probe reads endpoint/mix format without starting a stream. Actual RAW is recorded
only after successful IAudioClient initialization; native shared fallback is explicit.
"""
import ctypes as C
import threading
from .native_assets import native_library
from .native_host import HostSnapshot,HostRoute
class WasapiInfo(C.Structure):
 _fields_=[('size',C.c_uint32),('version',C.c_uint32),('rate',C.c_uint32),('channels',C.c_uint32),('bits',C.c_uint32),('is_float',C.c_uint32),('buffer_frames',C.c_uint32),('raw',C.c_uint32),('fallback_error',C.c_int32),('valid_bits',C.c_uint32),('channel_mask',C.c_uint32),('reserved',C.c_uint32),('clock_frequency',C.c_double),('endpoint',C.c_wchar*512),('name',C.c_char*256)]
 @classmethod
 def empty(cls):
  x=cls();x.size=C.sizeof(cls);x.version=1;return x

def values(x):
 return {k:(getattr(x,k).decode('utf8',errors='replace') if isinstance(getattr(x,k),bytes) else getattr(x,k)) for k,_ in x._fields_}

def probe_wasapi(endpoint=None,library=None):
 L=C.CDLL(str(library or native_library('astra_wasapi_host.dll')))
 L.astra_wasapi_probe.argtypes=[C.c_wchar_p,C.POINTER(WasapiInfo)];L.astra_wasapi_probe.restype=C.c_int32
 i=WasapiInfo.empty();hr=L.astra_wasapi_probe(endpoint,C.byref(i))
 if hr<0:raise RuntimeError(f'WASAPI endpoint probe failed: 0x{hr & 0xffffffff:08X}')
 return values(i)

class NativeWasapiHost:
 def __init__(self,core,library=None):
  self._lock=threading.RLock();self.core=core;self.lib=L=C.CDLL(str(library or native_library('astra_wasapi_host.dll')))
  L.astra_wasapi_create_owned.argtypes=[C.c_void_p]*4;L.astra_wasapi_create_owned.restype=C.c_void_p
  L.astra_wasapi_open.argtypes=[C.c_void_p,C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_int];L.astra_wasapi_open.restype=C.c_int
  for name,struct in [('snapshot',HostSnapshot),('route',HostRoute),('info',WasapiInfo)]:
   f=getattr(L,'astra_wasapi_'+name);f.argtypes=[C.c_void_p,C.POINTER(struct)];f.restype=C.c_int
  for name in ('close','destroy'):
   f=getattr(L,'astra_wasapi_'+name);f.argtypes=[C.c_void_p];f.restype=C.c_int
  with core._lock:
   if not core.handle:raise RuntimeError('core closed')
   callback=getattr(core.lib,'astra_core_qpc_callback',None)
   address=C.cast(callback,C.c_void_p).value if callback else core.callback_address
   self.handle=L.astra_wasapi_create_owned(address,core.handle,C.cast(core.lib.astra_core_retain,C.c_void_p),C.cast(core.lib.astra_core_release,C.c_void_p))
  if not self.handle:raise RuntimeError('native WASAPI host creation failed')
 def open(self,endpoint=None,rate=None,block=0,backend='wasapi-raw'):
  with self._lock:
   if not self.handle:raise RuntimeError('host closed')
   rate=self.core.sample_rate if rate is None else rate
   if rate!=self.core.sample_rate:raise ValueError('device rate must match prepared PCM rate')
   if backend not in ('auto','wasapi-raw','wasapi-shared'):raise ValueError('unsupported WASAPI backend')
   return bool(self.lib.astra_wasapi_open(self.handle,endpoint,int(rate),block,int(backend!='wasapi-shared')))
 def poll(self):
  with self._lock:
   if not self.handle:return {'state':5,'released':True}
   s=HostSnapshot();s.size=C.sizeof(s);s.version=1
   if not self.lib.astra_wasapi_snapshot(self.handle,C.byref(s)):raise RuntimeError('WASAPI snapshot ABI mismatch')
   return values(s)
 def route(self):
  with self._lock:
   if not self.handle:raise RuntimeError('host closed')
   r=HostRoute();r.size=C.sizeof(r);r.version=1
   if not self.lib.astra_wasapi_route(self.handle,C.byref(r)):raise RuntimeError('WASAPI route not active')
   i=WasapiInfo.empty()
   if not self.lib.astra_wasapi_info(self.handle,C.byref(i)):raise RuntimeError('WASAPI endpoint info unavailable')
   return dict(values(r),endpoint=i.endpoint,stream_rate=i.rate,clock_frequency=i.clock_frequency,channels=i.channels,bits=i.bits,valid_bits=i.valid_bits,channel_mask=i.channel_mask,buffer_frames=i.buffer_frames,fallback_error=i.fallback_error)
 def close(self):
  with self._lock:
   if self.handle:self.lib.astra_wasapi_close(self.handle)
 def finish_close(self):
  with self._lock:
   if not self.handle:return True
   if not self.poll()['released'] or not self.lib.astra_wasapi_destroy(self.handle):return False
   self.handle=None;self.core=None;return True
