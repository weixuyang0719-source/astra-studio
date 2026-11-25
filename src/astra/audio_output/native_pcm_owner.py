"""Experimental native PCM Handoff owner, not enabled by default.

Python only handles lifecycle and command receipts. PCM decode/prefetch, transport,
DAC mapping and device callbacks stay native. Dependencies are explicit; never
imports sounddevice or changes system device settings. Fixed .25-2x audio tempo;
1x is a transparent bypass, other rates preserve pitch in a native worker. No metronome.
The Tracks audition session can switch source on the same 1x device stream.
"""
import copy, math, os, queue, threading, time
from types import SimpleNamespace
from .native_stream import NativeAuditionCore, NativeStreamCache, NativeStreamCore
from .native_host import NativeDeviceHost
from .native_wasapi import NativeWasapiHost,probe_wasapi
from .native_tempo import NativeTempoCache,NativeTempoCore

_LIVE={}

class _NativePcmSession:
    def __init__(self,path,*,sndfile_dll,samplerate_dll,portaudio_dll,sample_rate,backend,device,buffer_frames,budget_bytes,timeline_duration=0,playback_rate=1,kept_endpoint=None):
        self.path=path;self.sndfile=sndfile_dll;self.samplerate=samplerate_dll;self.portaudio=portaudio_dll
        self.rate=sample_rate;self.backend=backend;self.device=device;self.block=buffer_frames;self.budget=budget_bytes
        self.timeline_duration=timeline_duration;self.tempo=playback_rate;self.tempo_cache=None
        self.kept_endpoint=kept_endpoint
        self.cache=None;self.caches=[];self.core=None;self.host=None;self.duration=0
        self._review_follower=None
    def open(self,position,cancel):
        if cancel.is_set():return False
        if self.kept_endpoint is None and self.backend!='asio':
            probed=probe_wasapi(self.device)
            self.rate=probed['rate'];self.device=probed['endpoint']
        if cancel.is_set():return False
        if not self._prepare_core(position,cancel):return False
        return self._open_device(cancel)

    def _open_device(self,cancel):
        if cancel.is_set():return False
        # Stream stays silent until the owner submits the initial playing intent.
        if self.kept_endpoint is not None:
            from .kept_endpoint import KeptDirectHost
            self.host=KeptDirectHost(self.kept_endpoint,self.core)
        else:
            self.host=NativeDeviceHost(self.core,self.portaudio) if self.backend=='asio' else NativeWasapiHost(self.core)
        # KeptEndpoint already owns ASIO initialization and ordered fallback.
        # Do not abandon that shared operation at an independent PCM deadline:
        # opening an ASIO driver can block in PortAudio longer than 20 seconds.
        # Only a verified endpoint fault or this owner's cancellation may end it.
        deadline=None if self.kept_endpoint is not None else time.monotonic()+20
        while not cancel.is_set():
            hs=self.host.poll()
            if hs['state']==1:break
            if hs['error'] or hs['released']:raise RuntimeError(hs['message'] or 'native host initialization failed')
            if deadline is not None and time.monotonic()>deadline:raise TimeoutError('native host initialization still pending')
            cancel.wait(.002)
        if cancel.is_set():return False
        if not self.host.open(self.device,self.rate,self.block,self.backend):raise RuntimeError('native host open request rejected')
        while not cancel.is_set():
            hs=self.host.poll()
            if hs['state']==3:return True
            if hs['error'] or hs['released']:raise RuntimeError(hs['message'] or 'native host open failed')
            if deadline is not None and time.monotonic()>deadline:raise TimeoutError('native host open still pending')
            cancel.wait(.002)
        return False
    def _load_cache(self, path):
        cache = NativeStreamCache()
        self.caches.append(cache)
        self.cache=cache
        cache.load(path, self.sndfile, self.samplerate, self.rate, self.budget)
        return cache

    def _prefetch_start(self, cache, position, cancel):
        # The owner is the request producer until the native callback starts.
        target = min(cache.frames, max(0, round(position * self.rate)))
        cache.request(target)
        deadline = time.monotonic() + 10
        while not cancel.is_set():
            status, _ = cache.read_for_test(target, 256)
            if status == 1:
                return True
            if status < 0:
                raise RuntimeError(cache.info()['error'] or 'native cache read failed')
            if time.monotonic() > deadline:
                raise TimeoutError('native first PCM block not ready')
            cancel.wait(.002)
        return False

    def _prepare_core(self, position, cancel):
        self.cache=self._load_cache(self.path)
        timeline_frames=max(self.cache.frames,math.ceil(self.timeline_duration*self.rate))
        self.duration=timeline_frames/self.rate
        if not self._prefetch_start(self.cache, position, cancel):return False
        if self.tempo==1:
            self.core=NativeStreamCore(self.cache,frames=timeline_frames)
        else:
            self.tempo_cache=NativeTempoCache(self.cache,self.tempo,frames=timeline_frames)
            output_target=min(self.tempo_cache.output_frames,max(0,round(position*self.rate/self.tempo)))
            self.tempo_cache.request(output_target,0);deadline=time.monotonic()+20
            while not cancel.is_set():
                status,_=self.tempo_cache.read_for_test(output_target,256)
                if status==1:break
                if status<0:raise RuntimeError(self.tempo_cache.info()['error'] or 'native tempo read failed')
                if time.monotonic()>deadline:raise TimeoutError('native tempo first block not ready')
                cancel.wait(.002)
            if cancel.is_set():return False
            self.core=NativeTempoCore(self.tempo_cache)
        return True

    def command(self,*args):return self.core.command(*args)
    def snapshot(self):
        if not self.core or not self.host:raise RuntimeError('native PCM session not open')
        hs=self.host.poll();s=self.core.snapshot();provider=self.core.provider_status()
        if hs['error']:raise RuntimeError(hs['message'] or 'native device failed')
        if provider['error']:raise RuntimeError('native PCM provider error '+str(provider['error']))
        if self.cache.info()['state']==3:raise RuntimeError(self.cache.info()['error'])
        if self.tempo_cache and self.tempo_cache.info()['state']==3:raise RuntimeError(self.tempo_cache.info()['error'])
        route=self.host.route()
        if self.backend=='asio' and hs.get('sample_rate')==self.rate:
            readback=getattr(self.host,'asio_parameters',None)
            if readback:hs['asio_driver_parameters']=readback(hs,route,self.device)
        s.update(device=hs['device'],output=hs,actual_route=route,provider=provider,duration=self.duration)
        return s
    def close(self,timeout=5):
        # A timeout never permits freeing the core or cache used by a late callback.
        if self.host:
            self.host.close();deadline=time.monotonic()+timeout
            while not self.host.finish_close():
                if time.monotonic()>deadline:return False
                time.sleep(.002)
            self.host=None
        if self._review_follower is not None:
            if not self._review_follower.close_follower():return False
            self._review_follower=None
        if self.core:self.core.close();self.core=None
        if self.tempo_cache:
            self.tempo_cache.close();deadline=time.monotonic()+timeout
            while not self.tempo_cache.finish_close():
                if time.monotonic()>deadline:return False
                time.sleep(.002)
            self.tempo_cache=None
        caches=self.caches or ((self.cache,) if self.cache else ())
        for cache in caches:
            cache.close()
        deadline=time.monotonic()+timeout
        for cache in caches:
            while not cache.finish_close():
                if time.monotonic()>deadline:return False
                time.sleep(.002)
        self.caches.clear()
        self.cache=None
        return True


class _NativeAuditionSession(_NativePcmSession):
    def __init__(self, paths, *, source, **options):
        super().__init__(paths[source], **options)
        self.paths = paths
        self.source = source

    def _prepare_core(self, position, cancel):
        for path in self.paths:
            if cancel.is_set():
                return False
            self._load_cache(path)
        self.cache = self.caches[self.source]
        timeline = max(math.ceil(self.timeline_duration * self.rate),
                       *(cache.frames for cache in self.caches))
        self.duration = timeline / self.rate
        if not self._prefetch_start(self.cache, position, cancel):
            return False
        self.core = NativeAuditionCore(self.caches, self.source, frames=timeline)
        return True


class _LatestPcmIntents:
    """A rapid drag keeps one merged intent, with the latest seek's own revision."""
    def __init__(self):
        self._condition = threading.Condition()
        self._pending = None

    def put(self, fields, revision):
        with self._condition:
            pending, _, seek = self._pending or ({}, 0, None)
            pending.update(fields)
            self._pending = (pending, revision, revision if 'position' in fields else seek)
            self._condition.notify()

    def get(self, timeout=0):
        with self._condition:
            if not self._condition.wait_for(lambda: self._pending is not None, timeout):
                raise queue.Empty
            item, self._pending = self._pending, None
            return item


def _heard_transport_playing(sample):
    flags=sample.get('reserved',0)
    return bool(flags&2) if flags&1 else bool(sample['audible_active'])


class NativePcmOwner:
    def __init__(self,paths,intent,generation,revision=0,*,sndfile_dll=None,samplerate_dll=None,portaudio_dll=None,sample_rate=48000,backend='wasapi-shared',device=None,buffer_frames=0,budget_bytes=8*1024*1024,muted=False,session_factory=None,timeline_duration=0,continuous_sources=False,defer_device=False,kept_endpoint=None):
        self.continuous_sources=continuous_sources
        self._kept_endpoint=kept_endpoint
        if defer_device and (not continuous_sources or backend!='asio' or intent.playing):
            raise ValueError('Device-free preparation requires a paused ASIO audition owner')
        self._defer_device=defer_device
        self._web_core=None
        self._device_start=threading.Event()
        self.paths=tuple(map(str,paths));self._intent=copy.deepcopy(vars(intent));self._validate(self._intent)
        if backend not in ('asio','auto','wasapi-raw','wasapi-shared'):raise ValueError('unsupported native PCM backend')
        if not isinstance(sample_rate,int) or not 8000<=sample_rate<=384000:raise ValueError('invalid device sample rate')
        if session_factory is None and (not all((sndfile_dll,samplerate_dll)) or (backend=='asio' and not portaudio_dll and kept_endpoint is None)):raise ValueError('explicit native runtime DLL paths required')
        if not math.isfinite(timeline_duration) or timeline_duration<0:raise ValueError('invalid timeline duration')
        timeline_duration=max(timeline_duration,self._intent.get('midi_duration',0))
        self.generation=generation;self.baseline_revision=revision;self._rate=sample_rate;self._source=intent.source;self._muted=muted
        self._kwargs=dict(sndfile_dll=sndfile_dll,samplerate_dll=samplerate_dll,portaudio_dll=portaudio_dll,sample_rate=sample_rate,backend=backend,device=device,buffer_frames=buffer_frames,budget_bytes=budget_bytes,timeline_duration=timeline_duration,playback_rate=self._intent.get('rate',1),kept_endpoint=kept_endpoint)
        self._factory=session_factory or (_NativeAuditionSession if continuous_sources else _NativePcmSession);self._session=None;self._failed_session=None
        self._lock=threading.RLock();self._closing=threading.Event();self._queue=_LatestPcmIntents() if continuous_sources else queue.Queue(256)
        self._pending_target=None
        self._review_click_target=(None,0.)
        self._state=dict(closed=False,output_released=False,playing=False,preparing=not defer_device,device_parked=defer_device,ended=False,position=intent.position,intent_revision=0,position_revision=0,error=None,ready=False,source=intent.source,sampled_at=time.perf_counter(),duration=timeline_duration)
        self._post_intent(self._intent.copy(),revision);_LIVE[id(self)]=self
        self._thread=threading.Thread(target=self._run,name='Native PCM control',daemon=True);self._thread.start()
    def _validate(self,d):
        if set(d)-{'mode','audition','midi_gain','position','source','gain','loop','playing','rate','metronome','midi_duration'}:raise ValueError('unknown PCM intent field')
        if d.get('audition','audio')!='audio':raise ValueError('native PCM owner supports audio only')
        if 'rate' in d and (not math.isfinite(d['rate']) or not .25<=d['rate']<=2):raise ValueError('native playback tempo outside .25..2')
        for k in ('position','gain','midi_gain','midi_duration'):
            if k in d and (not math.isfinite(d[k]) or d[k]<0):raise ValueError('invalid '+k)
        for k in ('gain','midi_gain'):
            if k in d and d[k]>1:raise ValueError('invalid '+k)
        if 'source' in d and (type(d['source'])is not int or not 0<=d['source']<len(self.paths)):raise ValueError('invalid source')
        if 'playing' in d and type(d['playing'])is not bool:raise ValueError('invalid playing')
        if 'loop' in d:
            a,b,on=d['loop']
            if type(on)is not bool or not all(math.isfinite(v) and v>=0 for v in (a,b)):raise ValueError('invalid loop')
        if 'metronome' in d and d['metronome'][1]:raise ValueError('native PCM metronome unsupported')
    def apply(self,delta,revision):
        d=copy.deepcopy(delta);self._validate(d)
        if 'rate' in d and d['rate']!=self._intent.get('rate',1):raise ValueError('tempo change requires owner handoff')
        if not self.continuous_sources and 'source' in d and d['source']!=self._source:raise ValueError('source change requires owner handoff')
        if not self.continuous_sources and 'midi_duration' in d and d['midi_duration']!=self._intent.get('midi_duration',0):raise ValueError('timeline duration change requires owner handoff')
        with self._lock:
            if self._closing.is_set():raise RuntimeError('PCM owner closing')
            self._post_intent(d,revision)
            if 'position' in d:self._pending_target=(d['position'],revision)
            if self._state['device_parked']:
                if d.get('playing') or 'position' in d or d.get('source',self._source)!=self._source:
                    self._state.update(preparing=True,device_parked=False)
                    self._device_start.set()
                else:
                    # Pausing a parked owner needs no device receipt. This also
                    # lets navigation discard a still-preparing project safely.
                    self._state['intent_revision']=revision
    def set_review_click(self,table,gain):
        with self._lock:
            if not self._closing.is_set():
                self._review_click_target=(table,gain)

    def _service_review_click(self,session):
        target=self._review_click_target
        if target[0] is not None and session._review_follower is None:
            from .native_pcm_midi_owner import _JointSession
            session._review_follower=_JointSession.follow_review(session,self._rate)
        follower=session._review_follower
        if follower is not None:
            table,gain=target
            follower.metronome(({'_review_click_table':table},True) if table is not None else (None,False))
            follower.midi_volume(0. if self._muted else gain)
            follower.service_midi(self._closing,lambda: self._closing.is_set() or target!=self._review_click_target)

    def _post_intent(self, fields, revision):
        if self.continuous_sources:
            self._queue.put(fields, revision)
        else:
            # The joint/MIDI subclass uses this established FIFO contract.
            self._queue.put_nowait((fields, revision))

    def _take_intents(self, timeout=0):
        if self.continuous_sources:
            return self._queue.get(timeout)
        fields, revision = self._queue.get(timeout=timeout)
        seek = revision if 'position' in fields else None
        for _ in range(256):
            try:
                delta, revision = self._queue.get_nowait()
            except queue.Empty:
                break
            fields.update(delta)
            if 'position' in delta:
                seek = revision
        return fields, revision, seek

    def _revoke_web(self):
        with self._lock:
            if self._web_core is not None:
                self._web_core.unbind_web_route()
                self._web_core=None
                self._state['web_route']=0

    def web_controls(self):
        with self._lock:
            return self._web_core.web_controls() if self._web_core else None

    def close(self):
        self._closing.set()
        self._device_start.set()
        self._revoke_web()
    def snapshot(self):
        with self._lock:
            # GUI reads cached state only; device/cache queries belong to worker.
            return SimpleNamespace(**self._state, pending_target=self._pending_target[0] if self._pending_target else None)
    def _update(self,s):
        playing=_heard_transport_playing(s)
        ended=s['audible_position']>=s['duration'] and not playing
        web=self._web_core.web_controls() if self._web_core else None
        wants_play=self._intent.get('playing') if web is None else web.playing
        preparing=bool(wants_play and not ended and not playing)
        if web is not None:
            self._state.update(web_serial=web.serial,web_command=web.command,web_ingress=web.ingress_ns,
                               web_heard=s['heard_transition'])
        if self.continuous_sources:
            self._state.update(source=s['heard_source'],duration=s['duration'],
                               output_overrange=s['output_overrange'],pre_headroom_peak=s['pre_headroom_peak'])
        self._state.update(applied=s.get('applied_commands'),epoch=s.get('audible_epoch'),rendered_epoch=s.get('rendered_epoch'))
        self._state.update(position=s['audible_position'],playing=playing,playhead_moving=bool(s['audible_active']),preparing=preparing,ended=ended,sampled_at=s['sampled_ns']/1e9,device=s.get('device',''),provider=s.get('provider',{}),rendered_position=s.get('rendered_position'),output=copy.deepcopy(s.get('output',{})),actual_route=copy.deepcopy(s.get('actual_route',{})))
    def _run(self):
        session=None
        try:
            if self.continuous_sources:
                session=self._factory(self.paths,source=self._source,**self._kwargs)
            else:
                session=self._factory(self.paths[self._source],**self._kwargs)
            # Preserve partial initialization for cleanup even if open() fails midway.
            with self._lock:self._session=session
            position=self._intent.get('position',0)
            if self._defer_device:
                if not session._prepare_core(position,self._closing):return
                self._device_start.wait()
                if self._closing.is_set() or not session._open_device(self._closing):return
            elif not session.open(position,self._closing):return
            self._rate=getattr(session,'rate',self._rate) # actual negotiated stream rate, before any frame commands
            while not self._closing.is_set():
                try:d,rev,position_rev=self._take_intents(timeout=.008 if self.continuous_sources else .02)
                except queue.Empty:
                    if getattr(session,'_review_follower',None) is not None or self._review_click_target[0] is not None:
                        try:self._service_review_click(session)
                        except Exception as exc:
                            with self._lock:self._state['review_click_error']=str(exc)
                    s=session.snapshot()
                    with self._lock:
                        if self._state['ready']:self._update(s)
                    continue
                def submit(fields):
                    commands=[]
                    if 'gain' in fields:commands.append((3,0 if self._muted else fields['gain'],0))
                    if 'loop' in fields:
                        a,b,on=fields['loop']
                        if on and max(a,b)>session.duration:raise ValueError('loop outside playback timeline')
                        commands.append((4,round(a*self._rate),round(b*self._rate)) if on else (5,0,0))
                    paired = self.continuous_sources and ('source' in fields or 'position' in fields)
                    if not paired and 'position' in fields:
                        commands.append((2,round(fields['position']*self._rate),0))
                    if not paired and 'playing' in fields:
                        commands.append((1,int(fields['playing']),0))
                    last = None
                    for cmd in commands:
                        if self._closing.is_set():break
                        last = session.command(*cmd)
                    transition = None
                    if paired and not self._closing.is_set():
                        target = round(fields['position']*self._rate) if 'position' in fields else -1
                        kind = {False: 7, True: 8}.get(fields.get('playing'), 6)
                        last = transition = session.command(kind, fields.get('source', -1), target)
                    return last, transition
                seq,transition=submit(d);deadline=time.monotonic()+5;seek_epoch=None;pause_boundary=None
                while seq is not None and not self._closing.is_set():
                    s=session.snapshot()
                    with self._lock:
                        if self._state['ready']:self._update(s)
                    # A DAC receipt must not block newer user intent. Supersede the
                    # unconfirmed target, but never publish a revision before its
                    # own rendered/audible receipt. In particular pause is sent now.
                    try:newer,newrev,newposrev=self._take_intents()
                    except queue.Empty:pass
                    else:
                        d.update(newer);rev=newrev
                        if newposrev is not None:position_rev=newposrev;seek_epoch=None
                        if 'loop' in newer:seek_epoch=None
                        if any(k in newer for k in ('playing','position','loop')):pause_boundary=None
                        next_seq,next_transition=submit(newer)
                        if next_seq is not None:seq=next_seq
                        if next_transition is not None:transition=next_transition
                        deadline=time.monotonic()+5
                        continue
                    if s['applied_commands']>=seq:
                        seek_ready=True
                        if transition is not None:
                            seek_ready=s['heard_transition']>=transition
                        elif 'position' in d and 'rendered_epoch' in s:
                            if seek_epoch is None:seek_epoch=s['rendered_epoch']
                            seek_ready=s['audible_epoch']>=seek_epoch
                        state_ready=True
                        if 'playing' in d:
                            if d['playing']:
                                state_ready=bool(s['audible_active']) or (s['audible_position']>=s['duration'] and not s.get('rendered_active',False))
                            else:
                                # An old silent DAC gap is not proof that the new
                                # pause tail has reached the device. Require its final
                                # rendered source point and epoch as well as inactivity.
                                # A paired seek can be applied before its target has
                                # reached the DAC. Never latch the old pause position.
                                if seek_ready and pause_boundary is None and not s.get('rendered_active',False):
                                    pause_boundary=(s.get('rendered_epoch',0),s.get('rendered_position',s['audible_position']))
                                state_ready=(pause_boundary is not None and not _heard_transport_playing(s) and not s['audible_active']
                                    and s.get('audible_epoch',0)>=pause_boundary[0]
                                    and abs(s['audible_position']-pause_boundary[1])<=1/self._rate)
                        if seek_ready and state_ready:break
                    if time.monotonic()>deadline:raise TimeoutError('native PCM seek audible receipt pending' if seek_epoch is not None else 'native PCM command receipt pending')
                    self._closing.wait(.002)
                if self._closing.is_set():break
                s=session.snapshot()
                with self._lock:
                    self._intent.update(d);self._state.update(ready=True,intent_revision=rev)
                    if 'position' in d:
                        self._state['position_revision']=position_rev
                        if self._pending_target and self._pending_target[1]==position_rev:self._pending_target=None
                    if (self.continuous_sources and self._web_core is None and isinstance(session.core,NativeAuditionCore)
                        and not self._closing.is_set() and not self._state.get('web_disabled')
                        and os.environ.get('ASTRA_NATIVE_WEB_TRANSPORT')=='1'):
                        self._state['web_route']=session.core.bind_web_route()
                        self._web_core=session.core
                    self._update(s)
        except Exception as e:
            with self._lock:self._state['error']=str(e)
        finally:
            self._revoke_web()
            last=None
            if session and self._state['ready']:
                try:last=session.snapshot()
                except Exception:pass
            with self._lock:
                if last is not None:self._update(last)
                self._session=None
            try:released=session is None or session.close()
            except Exception as e:
                released=False
                with self._lock:self._state['error']='native PCM close: '+str(e)
            with self._lock:
                self._failed_session=None if released else session
                if self._pending_target:self._state['position'],self._state['position_revision']=self._pending_target
                self._state.update(closed=True,output_released=released,playing=False,preparing=False,ready=False)
                if not released:self._state['error']='native PCM output release not confirmed'
            if not released and self._kept_endpoint is not None and session is not None:
                self._finish_late_shared_close(session)
            elif released:
                _LIVE.pop(id(self),None)

    def _finish_late_shared_close(self, session):
        # Keep cleanup on the control worker. The bounded close above can time out
        # during a late shared callback or a cache close; neither is permission to
        # start another owner, but the timeout must not discard the only ACK path.
        transient_errors = 0
        while True:
            time.sleep(.1)
            try:
                released = session.close(timeout=.1)
                transient_errors = 0
            except (OSError, RuntimeError) as exc:
                transient_errors += 1
                if transient_errors < 3:
                    continue
                with self._lock:
                    self._state['error'] = 'native PCM late release failed: ' + str(exc)
                return
            except Exception as exc:
                # No ACK: keep the failed session alive rather than assuming release.
                with self._lock:
                    self._state['error'] = 'native PCM late release failed: ' + str(exc)
                return
            if released:
                with self._lock:
                    self._failed_session = None
                    self._state['output_released'] = True
                _LIVE.pop(id(self),None)
                return
