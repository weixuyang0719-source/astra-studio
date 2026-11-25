"""Control-only PCM source preparation. No audio device and no Python RT callbacks."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ctypes as C
from pathlib import Path
from time import monotonic, sleep

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vst-pcm-source')

def dispose(cache):
    cache.close()
    while not cache.finish_close():
        sleep(.005)

def load_source(source, rate):
    from astra.audio_output.native_assets import native_dependencies
    from astra.audio_output.native_stream import NativeStreamCache
    path=Path(source['path']); identity=source['identity'];before=path.stat()
    if before.st_size!=identity['size'] or before.st_mtime_ns!=identity['mtime_ns']:
        raise ValueError('PCM source changed before preparation')
    deps=native_dependencies(need_portaudio=False);cache=NativeStreamCache()
    try:
        cache.load(path,deps['sndfile_dll'],deps['samplerate_dll'],rate)
        after=path.stat()
        if (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
            raise ValueError('PCM source changed during preparation')
        cache.request(0,1)
        return cache
    except BaseException:
        if cache.handle:dispose(cache)
        raise

class PcmSourceControl:
    def __init__(self,lane,rate,*,submit=None,loader=load_source,now=monotonic):
        self.lane,self.rate,self.submit,self.loader,self.now=lane,rate,submit or _POOL.submit,loader,now
        self.serial=0;self.wanted=None;self.epoch=None;self.active=None
        self.job=None;self.staged=None;self.ticket=None;self.closed=False
        self.view=dict(state='off',epoch=None,error=None)
        self.disposals=[]
    def _dispose(self,cache):
        if cache is not None:self.disposals.append(self.submit(dispose,cache))
    def set(self,source,epoch):
        if self.closed:raise RuntimeError('PCM control retired')
        if not isinstance(epoch,str) or not epoch:raise ValueError('PCM epoch required')
        if source is not None:
            source=deepcopy(source)
            if not isinstance(source,dict) or not isinstance(source.get('path'),str) or not Path(source['path']).is_absolute():raise ValueError('absolute PCM path required')
            if source.get('offset_seconds',0)!=0:raise ValueError('PCM v1 supports zero offset only')
            ident=source.get('identity',{})
            if any(type(ident.get(k)) is not int or ident[k]<0 for k in ('size','mtime_ns')):raise ValueError('PCM identity required')
            if not isinstance(source.get('key'),str):raise ValueError('PCM key required')
        if source==self.wanted and epoch==self.epoch and self.view['state']!='error':return dict(accepted=True,epoch=epoch)
        self.serial+=1;self.wanted,self.epoch=source,epoch
        self.view=dict(source or {});self.view.update(state='preparing',epoch=epoch,error=None,offset_seconds=0)
        # Existing source stays bound until a current prepared replacement has a grant.
        return dict(accepted=True,epoch=epoch)
    def poll(self,position):
        if self.closed:return
        self.disposals=[f for f in self.disposals if not f.done()]
        try:
            if self.job is not None:
                serial,future=self.job
                if not future.done():return
                self.job=None
                try:cache=future.result()
                except Exception:
                    if serial!=self.serial:return
                    raise
                if serial!=self.serial:self._dispose(cache);return
                self.staged=(serial,cache)
            if self.ticket is not None:
                serial,ticket,cache,deadline=self.ticket
                if not self.lane.control_ready(ticket):return
                if serial!=self.serial:
                    self.lane.end_control(ticket);self.ticket=None;self._dispose(cache);return
                if cache is not None:
                    cache.request(position,1)
                    ready,_=cache.read_for_test(position,256)
                    if ready==0:
                        if self.now()>deadline:raise RuntimeError('PCM preparation timed out at transport position')
                        return
                    if ready<0:raise RuntimeError('PCM source unavailable')
                    info=cache.info()
                    source=dict(handle=cache.handle,sample_rate=cache.sample_rate,frames=cache.frames,
                                request_address=cache.request_address,read_address=cache.read_address,
                                hint_address=C.cast(cache.lib.astra_cache_hint,C.c_void_p).value)
                else:source=None;info={}
                self.lane.bind_pcm(ticket,source)
                old,self.active=self.active,cache
                self.ticket=None
                # Once bound, retain new source even if resume reports failure.
                self._dispose(old)
                self.lane.end_control(ticket)
                self.view.update(state='ready' if cache else 'off',error=None,
                    source_rate=info.get('source_rate'),render_rate=info.get('sample_rate'),frames=info.get('frames'),misses=info.get('misses',0))
                return
            if self.view['state']!='preparing':return
            if self.staged is not None:
                serial,cache=self.staged
                if serial!=self.serial:self.staged=None;self._dispose(cache);return
                cache.request(position,1)
                ready,_=cache.read_for_test(position,256)
                if ready==0:return
                if ready<0:raise RuntimeError('PCM prefetch failed')
                ticket=self.lane.begin_control()['ticket']
                self.staged=None
                self.ticket=(serial,ticket,cache,self.now()+10.)
            elif self.wanted is None:
                self.ticket=(self.serial,self.lane.begin_control()['ticket'],None,self.now()+10.)
            else:
                self.job=(self.serial,self.submit(self.loader,deepcopy(self.wanted),self.rate))
        except Exception as exc:
            if self.ticket is not None:
                _,ticket,cache,_=self.ticket;self.ticket=None
                try:self.lane.end_control(ticket)
                except Exception:pass
                if cache is not self.active:self._dispose(cache)
            if self.staged is not None:
                self._dispose(self.staged[1]);self.staged=None
            self.view.update(state='error',error=str(exc) or type(exc).__name__)
    def status(self):return deepcopy(self.view)
    def retire(self):
        """Only after actual lane.close_ready proves final callback retirement."""
        self.closed=True
        caches=[self.active,self.staged[1] if self.staged else None,self.ticket[2] if self.ticket else None]
        self.active=self.staged=self.ticket=None
        for cache in {id(c):c for c in caches if c is not None}.values():self._dispose(cache)
        if self.job:
            _,future=self.job;self.job=None
            def done(f):
                try:self._dispose(f.result())
                except Exception:pass
            future.add_done_callback(done)
