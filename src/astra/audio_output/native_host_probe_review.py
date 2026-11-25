"""Isolated stream probe. Never changes endpoint settings or starts audio.
Caller owns returned IAudioClient** plus metadata and must use release_client.
The event handle is already configured; no Start is performed here.
RAW failures never silently fall back.
"""
import ctypes as C
import json
from soundcard.mediafoundation import _ffi, _com, _guidof
import soundcard as sc

class ClientInitializationError(RuntimeError):
    def __init__(self,original,cleanup_errors):
        self.original_error=original;self.cleanup_errors=tuple(cleanup_errors)
        self.output_released=not cleanup_errors
        super().__init__(repr(original)+('; cleanup: '+'; '.join(cleanup_errors) if cleanup_errors else ''))

class AudioClientProperties(C.Structure):
    _fields_=[('cbSize',C.c_uint32),('bIsOffload',C.c_int32),('eCategory',C.c_int32),('Options',C.c_uint32)]

def set_raw_properties(ptr,raw=False,category=0):
    """ptr must expose IAudioClient2/3, be uninitialized, and belong to this COM thread."""
    address=int(_ffi.cast('uintptr_t',ptr[0]))
    vt=C.cast(address,C.POINTER(C.POINTER(C.c_void_p))).contents
    method=C.WINFUNCTYPE(C.c_int32,C.c_void_p,C.POINTER(AudioClientProperties))(vt[16])
    props=AudioClientProperties(C.sizeof(AudioClientProperties),0,category,1 if raw else 0)
    hr=method(address,C.byref(props))
    if hr<0: raise OSError('SetClientProperties failed: 0x%08X'%(hr&0xffffffff))
    return dict(category=category,offload=False,options=props.Options,properties_size=props.cbSize)

def create_client(raw=False,category=0,flags=0x88040000|0x00100000,endpoint_id=None):
    """Activate IAudioClient3; exact stereo IEEE_FLOAT; native default period.
    RAW applies only to this stream. No Start, endpoint/global property writes.
    """
    device=None;ptr=None;event=None;device_release_attempted=False;device_release_failed=False
    try:
        speaker=sc.get_speaker(endpoint_id) if endpoint_id else sc.default_speaker()
        device=speaker._device_ptr();ptr=_ffi.new('IAudioClient **')
        iid=_guidof('{7ED4EE07-8E67-4CD4-8C1A-2B7A5987AD42}')
        hr=device[0][0].lpVtbl.Activate(device[0],iid,1,_ffi.NULL,_ffi.cast('void**',ptr))
        _com.check_error(hr)
        meta=set_raw_properties(ptr,raw,category)
        default=_ffi.new('REFERENCE_TIME*');minimum=_ffi.new('REFERENCE_TIME*')
        _com.check_error(ptr[0][0].lpVtbl.GetDevicePeriod(ptr[0],default,minimum))
        fmt=_ffi.new('WAVEFORMATEXTENSIBLE*')
        f=fmt[0].Format
        f.wFormatTag=3;f.cbSize=0;f.nChannels=2;f.nSamplesPerSec=48000
        f.nAvgBytesPerSec=384000;f.nBlockAlign=8;f.wBitsPerSample=32
        _com.check_error(ptr[0][0].lpVtbl.Initialize(ptr[0],0,flags,default[0],default[0],fmt,_ffi.NULL))
        count=_ffi.new('UINT32*');_com.check_error(ptr[0][0].lpVtbl.GetBufferSize(ptr[0],count))
        if flags & 0x40000:
            kernel=C.WinDLL('kernel32',use_last_error=True)
            kernel.CreateEventW.argtypes=[C.c_void_p,C.c_int,C.c_int,C.c_wchar_p]
            kernel.CreateEventW.restype=C.c_void_p
            event=kernel.CreateEventW(None,False,False,None)
            if not event: raise C.WinError(C.get_last_error())
            _com.check_error(ptr[0][0].lpVtbl.SetEventHandle(ptr[0],_ffi.cast('HANDLE',event)))
        meta['event_handle']=event
        meta.update(endpoint_id=speaker.id,device=speaker.name,interface='IAudioClient3',raw=raw,flags=flags,period_100ns=int(default[0]),minimum_period_100ns=int(minimum[0]),host_frames=int(count[0]),host_ms=count[0]/48,rate=48000,channels=2,format_tag=3,cbSize=0,limitation='separate stream, not private Qt client')
        device_release_attempted=True
        try:_com.release(device)
        except BaseException:
            device_release_failed=True;raise
        device=None
        return ptr,meta
    except BaseException as original:
        errors=[]
        if device_release_failed:errors.append('device Release outcome unknown')
        if ptr is not None and ptr[0]!=_ffi.NULL:
            try:_com.release(ptr)
            except BaseException as exc:errors.append('client Release: '+repr(exc))
        if event:
            try:_close_event(event)
            except BaseException as exc:errors.append('event CloseHandle: '+repr(exc))
        if device is not None and not device_release_attempted:
            try:_com.release(device)
            except BaseException as exc:errors.append('device Release: '+repr(exc))
        raise ClientInitializationError(original,errors) from original

def _close_event(event):
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CloseHandle.argtypes=[C.c_void_p]
    kernel.CloseHandle.restype=C.c_int
    if not kernel.CloseHandle(event): raise C.WinError(C.get_last_error())

def release_client(ptr,meta):
    # Caller must stop and join any feed thread before releasing.
    try: _com.release(ptr)
    finally:
        event=meta.pop('event_handle',None)
        if event: _close_event(event)

if __name__=='__main__':
    for raw in (False,True):
        try:
            ptr,meta=create_client(raw)
            try: print(json.dumps(meta,ensure_ascii=True))
            finally: release_client(ptr,meta)
        except Exception as e: print(json.dumps(dict(raw=raw,error=str(e))))
