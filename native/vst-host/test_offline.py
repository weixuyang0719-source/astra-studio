"""Native ABI smoke/regression tests using only the locally built official SDK fixture.
No audio device, user plugin, window or user project is opened.
Run with the project's Python after building astra_vst_host + mda-vst3.
"""
import ctypes as C
import json
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parents[2]
dll = C.CDLL(str(ROOT/'.cache/vst-host-build/Release/astra_vst_host.dll'))
class Event(C.Structure):
    _fields_=[('offset',C.c_uint32),('status',C.c_uint8),('a',C.c_uint8),('b',C.c_uint8),('reserved',C.c_uint8)]
P=C.c_void_p; U=C.c_uint32; Q=C.c_uint64; I=C.c_int32
for name,args,result in [
 ('abi_version',[],U),('last_error',[],C.c_char_p),('scan',[C.c_char_p,P,Q],Q),
 ('open',[C.c_char_p,C.c_char_p,C.c_double,U],P),
 ('process',[P,P,U,C.POINTER(Event),U,C.c_int64,C.c_double,U],I),
 ('get_state',[P,P,Q],Q),('set_state',[P,P,Q],I),('latency',[P],U),
 ('editor_attach',[P,P,C.POINTER(I),C.POINTER(I)],I),('editor_remove',[P],I),('close',[P],None)]:
 f=getattr(dll,'astra_vst_'+name);f.argtypes=args;f.restype=result
plugin=str(ROOT/'.cache/vst-host-build/VST3/Release/mda-vst3.vst3').encode()
assert dll.astra_vst_abi_version()==1
assert C.sizeof(Event)==8
buf=C.create_string_buffer(1024*1024)
n=dll.astra_vst_scan(plugin,buf,len(buf));assert n, dll.astra_vst_last_error()
classes=json.loads(buf.value)['classes']; assert len(classes)>10
piano=next(c for c in classes if c['name'].lower()=='mda piano')
print('Scanned',len(classes),'classes; instrument:',piano['name'],flush=True)
assert not dll.astra_vst_open(plugin,b'0'*32,48000,256)
assert not dll.astra_vst_open(plugin,piano['id'].encode(),float('nan'),256)
for cycle in range(3):
 h=dll.astra_vst_open(plugin,piano['id'].encode(),48000,256)
 assert h,dll.astra_vst_last_error()
 try:
  audio=np.zeros((256,2),np.float32)
  r=dll.astra_vst_process(h,audio.ctypes.data,256,None,0,0,120,1)
  assert r==0,r
  assert np.max(np.abs(audio))==0
  on=Event(64,0x90,60,100,0)
  assert dll.astra_vst_process(h,audio.ctypes.data,256,C.byref(on),1,256,120,1)==0
  assert np.max(np.abs(audio[:64]))==0, 'note sounded before sample offset'
  peak=float(np.max(np.abs(audio[64:])))
  assert peak>0.001,peak
  n=dll.astra_vst_get_state(h,None,0);assert n>56,dll.astra_vst_last_error()
  state=C.create_string_buffer(n);assert dll.astra_vst_get_state(h,state,n)==n
  assert dll.astra_vst_set_state(h,state,n)==0,dll.astra_vst_last_error()
  bad=C.create_string_buffer(state.raw);bad[8]=b'X';assert dll.astra_vst_set_state(h,bad,n)<0
  bad_event=Event(256,0x90,60,100,0)
  assert dll.astra_vst_process(h,audio.ctypes.data,256,C.byref(bad_event),1,512,120,1)==-2
  assert np.max(np.abs(audio))==0
  assert dll.astra_vst_process(h,audio.ctypes.data,257,None,0,512,120,1)==-1
  assert dll.astra_vst_editor_attach(h,None,None,None)<0
  assert dll.astra_vst_editor_remove(h)==0
  print('Cycle',cycle,'peak',peak,'state_bytes',n,'latency',dll.astra_vst_latency(h),flush=True)
 finally:
  dll.astra_vst_close(h)
assert dll.astra_vst_process(None,None,2**32-1,None,0,0,120,0)==-1
print('PASS: native scan/load/sample-offset audio/state/invalid input/lifecycle; no output device used')
