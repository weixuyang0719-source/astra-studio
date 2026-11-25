"""Isolated separate MIDI worker, reading ONE native played-position clock.
No audio-thread MIDI calls. Explicit pre-open readiness and fail-closed retirement.
Not connected to application routing.
"""
import threading,time,copy,collections,math
from dataclasses import dataclass,replace
@dataclass(frozen=True)
class MidiWorkerState:
 ready:bool=False
 closed:bool=False
 output_released:bool=False
 error:str|None=None
 busy_since:float|None=None
 max_step_ms:float=0.
 last_position:float=0.
 steps:int=0
 config_revision:int=-1
class CancelAwareSynth:
 def __init__(self,backend,cancel):self.backend=backend;self.cancel=cancel
 def device(self):return self.backend.device()
 def open(self):
  if self.cancel.is_set():raise OSError('MIDI close requested before open')
  return self.backend.open()
 def send(self,message):
  if not self.cancel.is_set():return self.backend.send(message)
 def reset(self):return self.backend.reset()
 def close(self):return self.backend.close()

class MidiClockWorker:
 def __init__(self,clock,factory,step_budget=.08):
  self.clock=clock;self.factory=factory;self.step_budget=step_budget
  self.lock=threading.Lock();self.status=MidiWorkerState(busy_since=time.perf_counter())
  self.stop=threading.Event();self.done=threading.Event();self.follower=None;self.pending_config=None
  self.thread=threading.Thread(target=self._run,name='Astra-MIDI-clock-worker',daemon=True);self.thread.start()
 def snapshot(self):
  with self.lock:return self.status
 def _set(self,**kw):
  with self.lock:self.status=replace(self.status,**kw)
 def configure(self,config,revision):
  # Already validated immutable transaction, no driver/Qt work on caller.
  with self.lock:self.pending_config=(config,revision)
 def close(self):self.stop.set()
 def _run(self):
  f=None;error=None;released=False;factory_started=False
  try:
   factory_started=True;f=self.factory();self.follower=f
   f.m._synth=CancelAwareSynth(f.m._synth,self.stop)
   if not self.stop.is_set():f.m._synth.open() # Ready only AFTER real synth opens, before audio start.
   self._base_duration=getattr(f.m,"duration",0.);self._set(ready=True,busy_since=None)
   while not self.stop.is_set():
    s=self.clock();start=time.perf_counter();self._set(busy_since=start)
    with self.lock:
     config=self.pending_config
     if config is not None and (getattr(s,'intent_revision',-1)<config[1] or s.playing):config=None
     if config is not None:self.pending_config=None
    if config is not None:
     values,revision=config
     f.m.pause();f.m._timer.stop()
     duration=max(self._base_duration,values.get('midi_duration',self._base_duration))
     if duration!=f.m.duration:f.m._duration=duration;f.m._metronome_signature=None
     f.m.master_position=min(s.position,duration);f.m._position=min(s.position,duration)
     f.m.set_metronome(*values.get('metronome',(None,False)))
     f.m.set_volume(round(values.get('midi_gain',1.)*100))
     f.epoch=None;f.playback_token=None # restore CC64/held notes at native position
     self._set(config_revision=revision)
    f.step(s)
    elapsed=time.perf_counter()-start
    self._set(busy_since=None,max_step_ms=max(self.snapshot().max_step_ms,elapsed*1000),last_position=s.position,steps=self.snapshot().steps+1)
    if f.error:raise RuntimeError(f.error)
    if elapsed>self.step_budget:raise TimeoutError('MIDI processing exceeded safe budget; playback must pause')
    self.stop.wait(.002)
  except BaseException as exc:error=repr(exc)
  finally:
   if f is not None:
    try:f.close();released=True
    except BaseException as exc:error=(error+'; ' if error else '')+'MIDI release: '+repr(exc)
   # A factory must own partial cleanup; unknown acquisition failure stays fail-closed.
   elif not factory_started:released=True
   self._set(ready=False,closed=True,output_released=released,error=error,busy_since=None)
   if released:self.follower=None
   self.done.set()

class NativeClockPair:
 """Independent supervisor; audio.apply must be nonblocking/transactional.
 Cancellation cannot interrupt a driver call already in flight. No replacement
 may acquire output until BOTH owners confirm release; cleanup reset is allowed.
 """
 def __init__(self,audio,factory,step_budget=.08,init_timeout=15):
  self.audio=audio;self.midi=MidiClockWorker(audio.snapshot,factory,step_budget)
  self.init_timeout=init_timeout;self.started=time.perf_counter();self.desired={};self.revision=0;self.dispatched=-1;self.error=None;self.closing=False
  self.required_config=-1
  self._lock=threading.RLock();self._stop=threading.Event();self.supervisor_done=threading.Event()
  self.supervisor=threading.Thread(target=self._supervise,name='Astra-joint-output-supervisor',daemon=True);self.supervisor.start()
 def _supervise(self):
  try:
   while not self._stop.wait(.002):self.service()
  except BaseException as exc:
   with self._lock:self._fail_locked('Supervisor failed: '+repr(exc))
  finally:self.supervisor_done.set()
 def apply(self,delta,revision):
  proposed=copy.deepcopy(delta)
  with self._lock:
   if self.closing or self.error:raise RuntimeError('pair closing or failed')
   if proposed.get('rate',1)!=1:raise ValueError('native joint preview supports only 1x; retire before fallback')
   if 'playing' in proposed and type(proposed['playing']) is not bool:raise ValueError('invalid playing')
   for key in ('midi_duration','midi_gain'):
    if key in proposed and (not math.isfinite(proposed[key]) or proposed[key]<0 or (key=='midi_gain' and proposed[key]>1)):raise ValueError('invalid '+key)
   if 'metronome' in proposed:
    tempo,enabled=proposed['metronome']
    if type(enabled) is not bool:raise ValueError('invalid metronome enabled')
    if enabled:
     from astra.timing import TempoMap
     TempoMap(tempo)
   desired={**self.desired,**proposed}
   config_changed=bool(set(proposed)&{'metronome','midi_duration','midi_gain'})
   state=self.midi.snapshot();ready=state.ready
   held_play=not ready or config_changed or getattr(state,'config_revision',-1)<self.required_config
   now={k:v for k,v in proposed.items() if k not in ('metronome','midi_duration','midi_gain')}
   if held_play:now['playing']=False
   # Native apply validates everything and atomically reserves a bounded queue
   # slot. Rejection leaves pair intent and revision untouched.
   self.audio.apply(now,revision)
   self.desired=desired;self.revision=revision
   if config_changed:
    self.required_config=revision
    self.midi.configure({k:desired[k] for k in ('metronome','midi_duration','midi_gain') if k in desired},revision)
   if not held_play:self.dispatched=revision
 def _fail_locked(self,problem):
  self.error=str(problem)
  try:self.audio.command('pause')
  except Exception:pass
  self._close_locked()
 def service(self):
  # Optional caller polling is harmless, but not needed for supervision.
  with self._lock:
   if self.closing:return
   m=self.midi.snapshot();a=self.audio.snapshot();now=time.perf_counter()
   problem=m.error or a.error
   if not problem and (m.closed or a.closed):problem='One side of joint transport closed unexpectedly'
   if not m.ready and not m.closed and now-self.started>self.init_timeout:problem='MIDI initialization timed out; output readiness unconfirmed'
   if m.ready and m.busy_since is not None and now-m.busy_since>self.midi.step_budget:problem='MIDI worker stalled; native audio retirement requested'
   if problem:self._fail_locked(problem);return
   if m.ready and getattr(m,'config_revision',-1)>=self.required_config and self.dispatched!=self.revision:
    try:self.audio.apply({'playing':self.desired.get('playing',False)},self.revision)
    except Exception as exc:self._fail_locked('Ready dispatch rejected: '+repr(exc));return
    self.dispatched=self.revision
 def _close_locked(self):
  if self.closing:return
  self.closing=True;self._stop.set()
  for owner in (self.midi,self.audio):
   try:owner.close()
   except Exception as exc:self.error=(self.error+'; ' if self.error else '')+'Close request failed: '+repr(exc)
 def close(self):
  with self._lock:self._close_locked()
 def released(self):
  a=self.audio.snapshot();m=self.midi.snapshot()
  return a.closed and a.output_released and m.closed and m.output_released
