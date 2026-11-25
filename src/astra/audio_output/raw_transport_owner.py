"""Experimental RAW transport: ONE owner thread owns device and timeline.

Not selected by the application. Public commands are asynchronous, and fallback

must wait for closed acknowledgement before creating any replacement output.

"""

import threading,time,collections,math,copy

from dataclasses import dataclass,replace,field

from .played_segment_timeline import PlayedSegmentTimeline as ContinuousTimeline
from .raw_pcm_click import PcmMetronome,PcmClickStatus

from astra.shared_audio_core import RATE

@dataclass(frozen=True)

class Status:

    accepted:int=0

    applied:int=0

    intent_revision:int=0

    epoch:int=0

    playback_token:tuple=(0,0)

    position:float=0.

    ended:bool=False

    playing:bool=False

    preparing:bool=False

    ready:bool=False

    closed:bool=False

    output_released:bool=False

    error:str|None=None

    source:int=0

    pending_target:float|None=None

    host_frames:int=0
    pcm_endpoint:dict=field(default_factory=dict)

    sampled_at:float=0.  # time.perf_counter() (QPC) when `position` was computed (owner thread)

    pcm_click:PcmClickStatus=PcmClickStatus()

class BackendInitializationError(RuntimeError):

    def __init__(self,message,output_released=False):super().__init__(message);self.output_released=output_released

class LateRelease(BackendInitializationError):

    """Initialisation (or close) timed out while the backend was still working. Release is NOT known
    now; release_event is set only after the late resource was really closed on its own thread."""

    def __init__(self,message,release_event):super().__init__(message,output_released=False);self.release_event=release_event

class ComApartment:

    """One balanced MTA reference owned by the calling audio thread."""

    def __init__(self,ole32=None):

        import ctypes

        self.thread=threading.get_ident();self.active=False

        self.ole=ole32 or ctypes.WinDLL('ole32')

        if ole32 is None:

            self.ole.CoInitializeEx.argtypes=[ctypes.c_void_p,ctypes.c_uint32]

            self.ole.CoInitializeEx.restype=ctypes.c_long

            self.ole.CoUninitialize.argtypes=[];self.ole.CoUninitialize.restype=None

        hr=int(self.ole.CoInitializeEx(None,0))

        if hr not in (0,1):raise BackendInitializationError(f'COM MTA initialization failed: 0x{hr & 0xffffffff:08x}',True)

        self.active=True

    def close(self):

        if not self.active:return

        if threading.get_ident()!=self.thread:raise RuntimeError('COM apartment released from wrong thread')

        self.ole.CoUninitialize();self.active=False



def actual_pcm_endpoint(device):
    # Only metadata reported by the successfully opened backend, never requests.
    meta=getattr(device,'meta',None)
    if not isinstance(meta,dict):return {}
    keys=('endpoint_id','device','interface','raw','rate','channels','host_frames')
    return {k:v for k in keys if (v:=meta.get(k)) is not None and type(v) in (str,bool,int,float)}


class NativeRaw:

    def __init__(self,endpoint=None,raw=True):

        try:

            from .native_host_probe_review import create_client,release_client

            from soundcard.mediafoundation import _Player,_ffi,_com

        except BaseException as exc:

            raise BackendInitializationError(repr(exc),output_released=True) from exc

        # SoundCard first import performs its own COM init and rejects S_FALSE.

        # Acquire our independently balanced reference after that import.

        self._apartment=ComApartment()

        self._release=release_client;self._ffi=_ffi;self._com=_com;self._ptr=None;self._render=None;self.meta={};self.closed=False

        created=False

        try:

            self._ptr,self.meta=create_client(raw=raw,endpoint_id=endpoint);created=True

            self.p=_Player.__new__(_Player);self.p._ptr=self._ptr;self.p.samplerate=RATE;self.p.channelmap=[0,1]

            self._render=self.p._render_client();self.p._ppRenderClient=self._render;self.capacity=self.p.buffersize

        except BaseException as exc:

            try:self.close()

            except BaseException as cleanup:raise BackendInitializationError(repr(exc)+'; cleanup: '+repr(cleanup),False) from exc

            raise BackendInitializationError(repr(exc),created or bool(getattr(exc,'output_released',False))) from exc

    def call(self,name):

        hr=getattr(self._ptr[0][0].lpVtbl,name)(self._ptr[0])

        if hr<0:self._com.check_error(hr)

    def padding(self):return self.p.currentpadding

    def write(self,pcm):

        n=len(pcm);ptr=self.p._render_buffer(n);self._ffi.memmove(ptr[0],pcm.tobytes(),pcm.nbytes);self.p._render_release(n)

    def start(self):self.call('Start')

    def stop(self):self.call('Stop')

    def reset(self):self.call('Reset')

    def close(self):

        if self.closed:return

        self.closed=True

        try:

            if self._ptr is not None:

                try:self.call('Stop')

                finally:

                    if self._render is not None:self._com.release(self._render);self._render=None

        finally:

            try:

                if self._ptr is not None:self._release(self._ptr,self.meta);self._ptr=None

            finally:

                self._apartment.close()

def loop_target(position, loop):
    if not loop:return position
    a,z=loop
    if (a<z and position>=z) or (a>z and z<=position<a):return a
    return position

def loop_repeats(loop):
    return bool(loop and loop[0]<loop[1])

def loop_landing_ready(cache, loop, capacity):
    if not loop:return True
    frame=loop[0]
    with cache.lock:
        return all((source,second) in cache.data for source in range(len(cache.paths))
                   for second in range(frame//RATE,(frame+capacity)//RATE+1))

class RawTransport:

    def __init__(self,cache,duration,backend_factory=NativeRaw,position=0.,source=0,command_limit=256,observer_factory=None):

        self._cache=cache;self._duration=duration;self._backend_factory=backend_factory;self._observer_factory=observer_factory;self._observer=None

        self._cv=threading.Condition();self._commands=collections.deque();self._limit=command_limit;self._closing=False;self._done=threading.Event()

        self._status=Status(position=position,source=source);self._initial=(position,source)

        self._thread=threading.Thread(target=self._run,name='Astra-RAW-owner-experiment',daemon=True);self._thread.start()

    def snapshot(self):

        with self._cv:return self._status

    def command(self,action,*args):

        if action not in {'play','pause','seek','source','loop','gain','close','device_changed'}:raise ValueError('unknown command')

        if action=='seek' and (len(args)!=1 or not math.isfinite(float(args[0]))):raise ValueError('invalid seek')

        if action=='gain' and (len(args)!=1 or not 0<=float(args[0])<=1):raise ValueError('invalid gain')

        if action=='loop':

            if len(args)!=3 or type(args[2])is not bool or not all(math.isfinite(float(v)) for v in args[:2]) or min(args[:2])<0 or max(args[:2])>self._duration:raise ValueError('invalid loop')

        with self._cv:

            if self._closing or self._status.closed:

                if action in {'close','device_changed'}:return self._status.accepted

                raise RuntimeError('transport closing')

            if action not in {'close','device_changed'} and len(self._commands)>=self._limit:raise BufferError('command queue full; nothing changed')

            seq=self._status.accepted+1;self._status=replace(self._status,accepted=seq)

            self._commands.append((seq,action,args))

            if action in {'close','device_changed'}:self._closing=True

            self._cv.notify();return seq

    def apply(self,delta,revision):

        # Validate the complete intent before reserving one queue slot.

        allowed={'mode','audition','midi_gain','position','source','gain','loop','playing','rate','metronome'}

        if set(delta)-allowed:raise ValueError('unknown intent field')

        if delta.get('audition','audio')!='audio':raise ValueError('native owner supports audio only')

        if 'rate' in delta and delta['rate']!=1:raise ValueError('native owner supports 1x only')

        commands=[]

        if 'source' in delta:

            v=delta['source']

            if type(v)is not int or not 0<=v<len(self._cache.paths):raise ValueError('invalid source')

            commands.append(('source',(v,)))

        if 'gain' in delta:

            v=delta['gain']

            if not math.isfinite(v) or not 0<=v<=1:raise ValueError('invalid gain')

            commands.append(('gain',(v,)))
        if 'midi_gain' in delta:
            v=delta['midi_gain']
            if not math.isfinite(v) or not 0<=v<=1:raise ValueError('invalid click gain')
            commands.append(('midi_gain',(v,)))
        if 'metronome' in delta:
            v=delta['metronome']
            if not isinstance(v,(tuple,list)) or len(v)!=2 or type(v[1])is not bool:raise ValueError('invalid metronome intent')
            # Snapshot only the authored metadata here. Validation and beat expansion run off-thread.
            commands.append(('metronome',(copy.deepcopy(v[0]),v[1])))
        if 'loop' in delta:

            v=delta['loop']

            if len(v)!=3 or type(v[2])is not bool or not all(math.isfinite(x) for x in v[:2]) or not (0<=v[0]<=self._duration and 0<=v[1]<=self._duration):raise ValueError('invalid loop')

            commands.append(('loop',tuple(v)))

        if 'position' in delta:

            v=delta['position']

            if not math.isfinite(v) or v<0:raise ValueError('invalid position')

            commands.append(('seek',(v,)))

        if 'playing' in delta:

            if type(delta['playing'])is not bool:raise ValueError('invalid playing')

            commands.append(('play' if delta['playing'] else 'pause',()))

        with self._cv:

            if self._closing or self._status.closed:raise RuntimeError('transport closing')

            if len(self._commands)>=self._limit:raise BufferError('command queue full; nothing changed')

            seq=self._status.accepted+1;self._status=replace(self._status,accepted=seq)

            self._commands.append((seq,'batch',(commands,revision)));self._cv.notify();return seq

    def close(self):

        return self.command('close')

    def wait_closed(self,timeout=5):return self._done.wait(timeout) # tests/background only, never GUI

    def _publish(self,**fields):

        if 'position' in fields:fields['sampled_at']=time.perf_counter()  # QPC, same clock as bridge transport_order

        with self._cv:self._status=replace(self._status,**fields);self._cv.notify_all()

    def _run(self):

        device=None;observer=None;observer_started=False;timeline=None;playback_token=(0,0);submitted=0;last_heard=[0];running=False;playing=False;hold=self._initial[0];epoch=0;applied=0;gain=1.;initialization_release_known=False;pending=None;pending_since=time.monotonic();error=None;source=self._initial[1]

        click=PcmMetronome(self._duration);applied_revision=0
        try:
            timeline=ContinuousTimeline(self._cache);timeline.source=timeline.old=source;timeline.seek(round(hold*RATE))

            device=self._backend_factory()

            if self._observer_factory is not None:

                observer_started=True;observer=self._observer_factory();self._observer=observer

            self._publish(ready=True,host_frames=device.capacity,pcm_endpoint=actual_pcm_endpoint(device))

            def heard():

                last_heard[0]=max(0,submitted-device.padding());return last_heard[0]

            def position():

                nonlocal playback_token

                frame,playback_token=timeline.played_cursor(heard() if submitted else 0)

                return min(self._duration,frame/RATE) if submitted else hold

            def reset_at(seconds):

                nonlocal submitted,running,hold,epoch

                device.stop();device.reset();running=False;submitted=0;hold=max(0.,min(self._duration,float(seconds)));timeline.seek(round(hold*RATE));epoch+=1
                click.discontinuity()

            def latest_unheard():

                if pending is not None:return pending[1]

                h=heard();future=[t/RATE for o,t,l in timeline.segments if o>h]

                return future[-1] if future else None

            quit_now=False

            while not quit_now:

                with self._cv:

                    commands=list(self._commands);self._commands.clear()

                expanded=[]

                for seq,action,args in commands:

                    if action=='batch':

                        batch,revision=args

                        expanded.extend((seq,a,v) for a,v in batch)

                        expanded.append((seq,'revision',(revision,)))

                    else:expanded.append((seq,action,args))

                for seq,action,args in expanded:

                    if action=='revision':
                        applied_revision=args[0];applied=seq;continue

                    if action in {'close','device_changed'}:

                        if action=='device_changed':error='RAW output device changed; ordinary playback fallback required'

                        applied=seq;quit_now=True;break

                    if action=='seek':

                        self._publish(ended=False)

                        target=max(0.,min(self._duration,float(args[0])))

                        if timeline.loop and playing:target=loop_target(target,tuple(x/RATE for x in timeline.loop))

                        if playing and running:pending=(seq,target);pending_since=time.monotonic()

                        else:reset_at(target);pending=None

                    elif action=='pause':

                        target=latest_unheard();device.stop();running=False;playing=False

                        if target is not None:reset_at(target);pending=None

                        else:hold=position()

                    elif action=='play':

                        self._publish(ended=False)

                        if position()>=self._duration:reset_at(0)

                        current=position();target=loop_target(current,tuple(x/RATE for x in timeline.loop) if timeline.loop else None)
                        if target!=current:reset_at(target)
                        playing=True;pending_since=time.monotonic()
                        # Resume retained endpoint frames even when the buffer is full.
                        # Waiting for free space while stopped can never make progress.
                        if not running and submitted and device.padding()>0:
                            device.start();running=True

                    elif action=='source':

                        target=int(args[0]);timeline.select(target);source=target

                        if not playing:reset_at(hold)

                    elif action=='gain':gain=float(args[0])
                    elif action=='midi_gain':click.set_gain(float(args[0]))
                    elif action=='metronome':click.request(*args)
                    elif action=='loop':

                        a,z,on=args

                        if on and max(a,z)>self._duration:raise ValueError('loop outside source')

                        pos=latest_unheard()

                        if pos is None:pos=position()

                        timeline.loop=(round(a*RATE),round(z*RATE)) if on and round(a*RATE)!=round(z*RATE) else None

                        if timeline.loop and playing:pos=loop_target(pos,(a,z))

                        reset_at(pos);pending=None

                    applied=seq

                if quit_now:break
                click.poll()

                # Only owner mutates timeline, so render/seek/reset/write form one serialized transaction.

                frames=[timeline.cursor]+([timeline.loop[0]] if timeline.loop else [])+([round(pending[1]*RATE)] if pending else [])

                self._cache.protect({(i,s) for f in frames for i in range(len(self._cache.paths)) for s in range(max(0,f//RATE),max(0,f//RATE)+2)})

                self._cache.prefetch(timeline.cursor)

                if timeline.loop:self._cache.prefetch(timeline.loop[0])

                if pending is not None:

                    _,target=pending;f=round(target*RATE);self._cache.prefetch(f)

                    with self._cache.lock:

                        target_ready=all((i,s) in self._cache.data for i in range(len(self._cache.paths)) for s in range(f//RATE,(f+device.capacity)//RATE+1))

                    if target_ready:

                        timeline.jump(f);click.discontinuity();pending=None

                    elif time.monotonic()-pending_since>5:raise TimeoutError('RAW seek data not ready')

                if playing:

                    if not loop_repeats(timeline.loop) and position()>=self._duration:

                        playing=False;reset_at(self._duration);self._publish(ended=True)

                    else:

                        available=device.capacity-device.padding()
                        if not running and not loop_landing_ready(self._cache,timeline.loop,device.capacity):
                            if time.monotonic()-pending_since>5:raise TimeoutError('RAW loop landing data not ready')
                            available=0

                        if available:

                            try:pcm=click.render(timeline,available,gain)
                            except BufferError:

                                if running:raise

                                if time.monotonic()-pending_since>5:raise TimeoutError('RAW initial data not ready')

                            else:

                                device.write(pcm);click.submitted();submitted+=available

                                if not running:device.start();running=True

                if self._cache.failures:raise RuntimeError(str(self._cache.failures))

                current=position() if playing else hold

                if not submitted:_,playback_token=timeline.played_cursor(0)

                self._publish(applied=applied,intent_revision=applied_revision,epoch=epoch,playback_token=playback_token,position=current,playing=playing,preparing=playing and not running,source=source,pending_target=pending[1] if pending else None,pcm_click=click.status(gain))

                if observer is not None:

                    observer.step(self.snapshot())

                    if getattr(observer,'error',None):raise RuntimeError('MIDI follower: '+str(observer.error))

                with self._cv:

                    if not self._commands:self._cv.wait(.002 if playing else .02)

        except BaseException as exc:

            error=repr(exc);initialization_release_known=bool(getattr(exc,'output_released',False))

        finally:

            click.close()
            preserved=hold if not playing else self._status.position

            if pending is not None:preserved=pending[1]

            elif timeline is not None:

                future=[t/RATE for o,t,l in timeline.segments if o>last_heard[0]]

                if future:preserved=future[-1]

            released=False

            try:

                if device is not None:device.close();released=True

                else:released=initialization_release_known

            except BaseException as exc:error=(error+'; ' if error else '')+'cleanup: '+repr(exc)

            finally:

                if observer is not None:

                    try:observer.close()

                    except BaseException as exc:

                        released=False;error=(error+'; ' if error else '')+'MIDI cleanup: '+repr(exc)

                elif observer_started:

                    released=released and initialization_release_known

                if released:self._observer=None

                # Closed is published only AFTER native output and handles are released.

                self._publish(applied=applied,intent_revision=applied_revision,epoch=epoch,position=preserved,playing=False,preparing=False,closed=True,output_released=released,error=error,source=source,pending_target=None,pcm_click=click.status(gain))

                self._done.set()

