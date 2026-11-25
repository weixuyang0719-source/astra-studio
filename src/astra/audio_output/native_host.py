"""Native device host control ABI. No Python audio callback.

Explicit experimental use only. Construction loads drivers on a native owner thread.
The host retains core PCM ownership and this object keeps the core DLL module alive
until confirmed release. close() requests shutdown; poll() confirms it, finish_close()
releases the host only after confirmation. Never unload or replace on a timeout.
"""
from .native_assets import native_library
import ctypes as C
import math
from pathlib import Path
import threading

class HostSnapshot(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32),('state',C.c_uint32),('released',C.c_uint32),('error',C.c_int32),('observed_frames',C.c_uint32),('callbacks',C.c_uint64),('status_flags',C.c_uint64),('sample_rate',C.c_double),('output_latency',C.c_double),('current_time',C.c_double),('dac_time',C.c_double),('device',C.c_char*256),('message',C.c_char*512)]

class HostRoute(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32),('host_type',C.c_int32),('device_index',C.c_int32),('shared',C.c_int32),('raw',C.c_int32),('host',C.c_char*128)]

class NativeDeviceHost:
    def __init__(self,core,portaudio_dll,library=None):
        self._lock=threading.RLock();self.core=core
        self._asio_readback=None
        path=library or native_library('astra_portaudio_host.dll')
        self.lib=C.CDLL(str(path));L=self.lib
        L.astra_host_create_owned.argtypes=[C.c_wchar_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p];L.astra_host_create_owned.restype=C.c_void_p
        L.astra_host_open.argtypes=[C.c_void_p,C.c_char_p,C.c_double,C.c_uint32];L.astra_host_open.restype=C.c_int
        L.astra_host_open_backend.argtypes=[C.c_void_p,C.c_int32,C.c_char_p,C.c_double,C.c_uint32];L.astra_host_open_backend.restype=C.c_int
        L.astra_host_close.argtypes=[C.c_void_p];L.astra_host_close.restype=C.c_int
        L.astra_host_snapshot.argtypes=[C.c_void_p,C.POINTER(HostSnapshot)];L.astra_host_snapshot.restype=C.c_int
        L.astra_host_route.argtypes=[C.c_void_p,C.POINTER(HostRoute)];L.astra_host_route.restype=C.c_int
        L.astra_host_destroy.argtypes=[C.c_void_p];L.astra_host_destroy.restype=C.c_int
        # Serialize create/retain against control-side core close.
        with core._lock:
            if not core.handle:raise RuntimeError('core closed')
            self.handle=L.astra_host_create_owned(str(Path(portaudio_dll).resolve()),core.callback_address,core.handle,C.cast(core.lib.astra_core_retain,C.c_void_p),C.cast(core.lib.astra_core_release,C.c_void_p))
        if not self.handle:raise RuntimeError('native host creation rejected')
    def open(self,driver=None,rate=None,block=0,backend="asio"):
        with self._lock:
            if not self.handle:raise RuntimeError('host closed')
            if rate is None:rate=self.core.sample_rate
            if rate!=self.core.sample_rate:raise ValueError('device rate must match prepared core PCM rate')
            if backend not in ('asio','wasapi-shared'):raise ValueError('unsupported native backend')
            self._asio_readback=None
            return bool(self.lib.astra_host_open_backend(self.handle,3 if backend=='asio' else 13,driver.encode('utf8') if driver else None,rate,block))
    def poll(self):
        with self._lock:
            if not self.handle:return {'released':True,'state':5}
            s=HostSnapshot();s.size=C.sizeof(s);s.version=1
            if not self.lib.astra_host_snapshot(self.handle,C.byref(s)):raise RuntimeError('snapshot ABI failure')
            return {k:(getattr(s,k).decode('utf8',errors='replace') if isinstance(getattr(s,k),bytes) else getattr(s,k)) for k,_ in s._fields_}
    def asio_parameters(self,sample,route,expected):
        with self._lock:
            base=asio_open_parameters(None,self.handle,sample,route,expected)
            if base is None:return None
            key=tuple(base.items())
            if self._asio_readback is None or self._asio_readback[0]!=key:
                self._asio_readback=key,asio_open_parameters(self.lib,self.handle,sample,route,expected)
            return self._asio_readback[1]
    def route(self):
        with self._lock:
            if not self.handle:raise RuntimeError('host closed')
            r=HostRoute();r.size=C.sizeof(r);r.version=1
            if not self.lib.astra_host_route(self.handle,C.byref(r)):raise RuntimeError('route unavailable')
            result={k:(getattr(r,k).decode('utf8',errors='replace') if isinstance(getattr(r,k),bytes) else getattr(r,k)) for k,_ in r._fields_}
            get=getattr(self.lib,'astra_host_asio_control',None)
            if get and r.host_type==3:
                get.argtypes=[C.c_void_p,C.POINTER(AsioControlInfo)];get.restype=C.c_int
                info=AsioControlInfo();info.size=C.sizeof(info);info.version=1
                if get(self.handle,C.byref(info)) and info.state==2 and not info.error:
                    result['buffer_info']={key:getattr(info,key) for key in ('min_frames','max_frames','preferred_frames','granularity')}
            return result
    def close(self):
        with self._lock:
            if self.handle:self.lib.astra_host_close(self.handle)
    def finish_close(self):
        with self._lock:
            if not self.handle:return True
            if not self.poll()['released']:return False
            if not self.lib.astra_host_destroy(self.handle):return False
            self.handle=None;self.core=None;return True


class AsioControlInfo(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32),('state',C.c_uint32),('error',C.c_int32),('min_frames',C.c_int32),('max_frames',C.c_int32),('preferred_frames',C.c_int32),('granularity',C.c_int32),('request_id',C.c_uint64),('completed_id',C.c_uint64),('device',C.c_char*256)]

def asio_open_parameters(host,handle,sample,route,expected):
    # No driver call: all values come from the native owner's existing
    # Pa_GetStreamInfo and PaAsio_GetAvailableBufferSizes snapshots.
    def get(record,key):return record.get(key) if isinstance(record,dict) else getattr(record,key,None)
    def text(value):return value.decode('utf8',errors='replace') if isinstance(value,bytes) else value
    rate=get(sample,'sample_rate')
    if (not handle or not expected or get(sample,'state') not in (2,3)
            or get(sample,'released') or get(sample,'error')
            or get(route,'host_type')!=3 or get(route,'device_index') is None
            or get(route,'device_index')<0 or get(route,'raw')
            or text(get(sample,'device'))!=expected
            or type(rate) not in (int,float) or not math.isfinite(rate) or not 8000<=rate<=384000):
        return None
    latency=get(sample,'output_latency')
    value=dict(proof='opened-asio-stream',device=expected,sample_rate=rate)
    if type(latency) in (int,float) and math.isfinite(latency) and 0<latency<10:
        value['output_latency']=latency
    getter=getattr(host,'astra_host_asio_control',None)
    if getter is not None:
        control=AsioControlInfo();control.size=C.sizeof(control);control.version=1
        # Optional metadata on older device-host alternatives; not a readiness gate.
        try:
            getter.argtypes=[C.c_void_p,C.POINTER(AsioControlInfo)];getter.restype=C.c_int
            if (getter(handle,C.byref(control)) and control.state==2 and control.error==0
                    and text(control.device)==expected and 0<control.preferred_frames<=65536):
                value['preferred_frames']=control.preferred_frames
        except (AttributeError,OSError,TypeError,ValueError):pass
    return value


class NativeAsioControl:
    """Explicit user-requested control UI/query, only after previous output release.

    Never opens an audio stream. All driver calls stay on the native COM owner
    thread. A modal driver panel may block that thread: close stays pending until
    it returns; callers must NOT treat a timeout as release or start another owner.
    """
    def __init__(self,portaudio_dll,library=None):
        self._lock=threading.RLock();self.lib=L=C.CDLL(str(library or native_library('astra_portaudio_host.dll')))
        L.astra_host_control_create.argtypes=[C.c_wchar_p];L.astra_host_control_create.restype=C.c_void_p
        L.astra_host_asio_request.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t,C.c_int];L.astra_host_asio_request.restype=C.c_int
        L.astra_host_asio_control.argtypes=[C.c_void_p,C.POINTER(AsioControlInfo)];L.astra_host_asio_control.restype=C.c_int
        L.astra_host_snapshot.argtypes=[C.c_void_p,C.POINTER(HostSnapshot)];L.astra_host_snapshot.restype=C.c_int
        for name in ('close','destroy'):
            f=getattr(L,'astra_host_'+name);f.argtypes=[C.c_void_p];f.restype=C.c_int
        self.handle=L.astra_host_control_create(str(Path(portaudio_dll).resolve()))
        if not self.handle:raise RuntimeError('ASIO output still owned or control host unavailable')
    def poll(self):
        with self._lock:
            if not self.handle:return dict(state=5,released=True,control_state=0)
            h=HostSnapshot();h.size=C.sizeof(h);h.version=1;c=AsioControlInfo();c.size=C.sizeof(c);c.version=1
            if not self.lib.astra_host_snapshot(self.handle,C.byref(h)) or not self.lib.astra_host_asio_control(self.handle,C.byref(c)):raise RuntimeError('ASIO control ABI mismatch')
            out={k:(getattr(h,k).decode('utf8',errors='replace') if isinstance(getattr(h,k),bytes) else getattr(h,k)) for k,_ in h._fields_}
            out.update(control_state=c.state,control_error=c.error,request_id=c.request_id,completed_id=c.completed_id,buffer_info=dict(min_frames=c.min_frames,max_frames=c.max_frames,preferred_frames=c.preferred_frames,granularity=c.granularity,device=c.device.decode('utf8',errors='replace')))
            return out
    def _request(self,driver,parent,show):
        with self._lock:
            if not self.handle:raise RuntimeError('control host closed')
            return bool(self.lib.astra_host_asio_request(self.handle,driver.encode('utf8'),parent,int(show)))
    def show_panel(self,driver,parent_hwnd=0):return self._request(driver,parent_hwnd,True)
    def query_buffers(self,driver):return self._request(driver,0,False)
    def buffer_info(self):return self.poll()['buffer_info']
    def close(self):
        with self._lock:
            if self.handle:self.lib.astra_host_close(self.handle)
    def finish_close(self):
        with self._lock:
            if not self.handle:return True
            if not self.poll()['released'] or not self.lib.astra_host_destroy(self.handle):return False
            self.handle=None;return True
