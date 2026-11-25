"""Offline regression for mapped MIDI parameter queues; no audio device is opened."""
import argparse
import ctypes as C
import json
from pathlib import Path

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--build-dir',type=Path,default=Path(__file__).resolve().parents[2]/'.cache/vst-host-build')
    ap.add_argument('--configuration',default='Release')
    args=ap.parse_args()
    dll=C.CDLL(str(args.build_dir/args.configuration/'astra_vst_host.dll'))
    class Event(C.Structure):
        _fields_=[('offset',C.c_uint32),('status',C.c_uint8),('a',C.c_uint8),('b',C.c_uint8),('reserved',C.c_uint8)]
    P=C.c_void_p;U=C.c_uint32;Q=C.c_uint64
    dll.astra_vst_scan.argtypes=[C.c_char_p,P,Q];dll.astra_vst_scan.restype=Q
    dll.astra_vst_open.argtypes=[C.c_char_p,C.c_char_p,C.c_double,U];dll.astra_vst_open.restype=P
    dll.astra_vst_process.argtypes=[P,P,U,C.POINTER(Event),U,C.c_int64,C.c_double,U];dll.astra_vst_process.restype=C.c_int32
    dll.astra_vst_close.argtypes=[P];dll.astra_vst_close.restype=None
    plugin=str(args.build_dir/'VST3'/args.configuration/'mda-vst3.vst3').encode()
    buf=C.create_string_buffer(1024*1024)
    if not dll.astra_vst_scan(plugin,buf,len(buf)):raise RuntimeError('fixture scan failed')
    piano=next(c for c in json.loads(buf.value)['classes'] if c['name'].lower()=='mda piano')
    h=dll.astra_vst_open(plugin,piano['id'].encode(),48000,4096)
    if not h:raise RuntimeError('fixture open failed')
    try:
        for count in (256,257,4096):
            events=(Event*count)(*(Event(i,0xB0,64,127 if i%2 else 0,0) for i in range(count)))
            audio=(C.c_float*(4096*2))()
            result=dll.astra_vst_process(h,audio,4096,events,count,0,120,1)
            if result!=0:raise AssertionError(f'{count} mapped CC64 points returned {result}')
            print(f'PASS mapped CC64 queue: {count} sample-offset points')
    finally:dll.astra_vst_close(h)

if __name__=='__main__':main()

