"""Cold instrument-only FFI. The new C++ operation runs on the candidate's existing VST UI owner thread."""
import ctypes as C
from pathlib import Path
from .native import HostError, Plugin

U=C.c_uint32
class _Receipt(C.Structure):
    _fields_=[('bytes',U),('version',U),('attempts',U),('chosen',U),('result',U),('capacity',U*2),
              ('outcome',U*2),('reserved',U),('attempt_us',C.c_uint64*2),('total_us',C.c_uint64),('error',C.c_char*512)]


def open_instrument(host,path,cid,rate,wide,requested,observed):
    dll=C.CDLL(str(Path(__file__).resolve().parent/'_native'/'astra_instrument_start.dll'))
    run=dll.astra_instrument_start_v1
    run.argtypes=[C.c_void_p,C.c_void_p,C.c_char_p,C.c_char_p,C.c_double,U,U,U,C.POINTER(_Receipt)]
    run.restype=C.c_void_p
    r=_Receipt(C.sizeof(_Receipt),1)
    h=run(C.cast(host.dll.astra_vst_open,C.c_void_p),C.cast(host.dll.astra_vst_last_error,C.c_void_p),
          str(Path(path).resolve()).encode('utf8'),cid.upper().encode('ascii'),rate,wide,requested,observed,C.byref(r))
    record=dict(version=1,selected=r.chosen,attempts=[
        dict(capacity=r.capacity[i],outcome=('ok','invalid','setup_rejected','other')[r.outcome[i]],us=r.attempt_us[i])
        for i in range(r.attempts)],native_total_us=r.total_us)
    if not h:
        exc=HostError(r.error.decode('utf8','replace') or 'Native instrument preparation failed')
        exc.instrument_startup=record
        raise exc
    details = getattr(host.dll, 'astra_vst_open_times_us_v1', None)
    if details is not None:
        labels = ('module','initialize','buses','controller_state','midi_map','setup','buffers','activate')
        times = (C.c_uint64 * len(labels))()
        details.argtypes = [C.c_void_p, C.POINTER(C.c_uint64), U]
        details.restype = None
        details(h, times, len(labels))
        record['stages_us'] = dict(zip(labels, times))
    return Plugin(host,h,r.chosen),record
