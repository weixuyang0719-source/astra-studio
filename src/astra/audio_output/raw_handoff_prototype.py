"""GUI-thread-only experimental output handoff arbiter.
Every playable entry point must express intent here. Never mix direct legacy
play() calls with this controller. Factories and owner.close() must not block.
"""
from dataclasses import dataclass,replace
@dataclass(frozen=True)
class Intent:
 mode:str='ordinary'
 audition:str='audio'
 playing:bool=False
 position:float=0.
 source:int=0
 gain:float=1.
 midi_gain:float=1.
 loop:tuple=(0.,0.,False)
 rate:float=1.
 metronome:tuple=(None,False)
 midi_duration:float=0.
class Handoff:
 def __init__(self,factories,intent=Intent(),*,allow_fallback=True):
  self.allow_fallback=allow_fallback
  self._web_stamp=(0,0);self._web_seek=0
  self.factories=factories;self.intent=intent;self.owner=None;self.owner_mode=None;self.owner_generation=0;self.revision=0;self.seek_revision=0;self.retiring=False;self.blocked=None;self.closing=False;self.events=[];self._retire_seek_revision=0;self._preserve_retired_position=True;self._pause_before_retire=None
 def sync_web_intent(self):
  owner=self.owner
  sample=getattr(owner,'web_controls',None)
  if self.retiring or self.closing or sample is None:return
  controls=sample()
  if controls is None or not controls.serial:return
  stamp=self.owner_generation,controls.serial
  if stamp==self._web_stamp:return
  if stamp[0]!=self._web_stamp[0]:self._web_seek=0
  self._web_stamp=stamp
  delta=dict(playing=bool(controls.playing),source=controls.source)
  if controls.seek>self._web_seek:
   self._web_seek=controls.seek;delta['position']=controls.target
  self.intent=replace(self.intent,**delta)
 def change(self,**delta):
  self.sync_web_intent()
  if self.closing:raise RuntimeError('session closing')
  if self.blocked:raise RuntimeError(self.blocked)
  if 'mode' in delta and delta['mode'] not in self.factories:raise ValueError('unknown output mode')
  if 'playing' in delta and type(delta['playing']) is not bool:raise ValueError('invalid playing state')
  candidate=replace(self.intent,**delta)
  import math
  if not math.isfinite(candidate.position) or candidate.position<0:raise ValueError('invalid position')
  if candidate.audition not in ('audio','midi','joint') or not 0<=candidate.midi_gain<=1:raise ValueError('invalid audition/MIDI gain')
  if not 0<=candidate.gain<=1 or not .25<=candidate.rate<=2:raise ValueError('invalid gain/rate')
  next_revision=self.revision+1
  same_owner=self.owner and not self.retiring and self.owner_mode==candidate.mode
  if same_owner:
   self._pause_before_retire=None
   try:self.owner.apply(delta,next_revision)
   except Exception as exc:
    self.blocked='Intent application unconfirmed: '+repr(exc);self._retire(False);self.poll();raise RuntimeError(self.blocked) from exc
  self.revision=next_revision
  if 'position' in delta:self.seek_revision=self.revision
  self.intent=candidate
  if self.owner and not self.retiring and not same_owner:
   preserve='position' not in delta
   if getattr(self.owner,'continuous_sources',False) and not candidate.playing:
    self._drain_before_retire(preserve)
   else:self._retire(preserve)
  self.poll()
 def toggle(self):
  self.poll()
  self.change(playing=not self.intent.playing)
 def _drain_before_retire(self,preserve_position):
  if self._pause_before_retire is None:
   try:self.owner.apply({'playing':False},self.revision)
   except RuntimeError:
    # Its worker may have failed concurrently. Poll the failure/release path.
    self._retire(preserve_position)
    return
   self._pause_before_retire=(self.revision,preserve_position,self.seek_revision)
  elif not preserve_position:
   revision,_,seek=self._pause_before_retire
   self._pause_before_retire=(revision,False,seek)
 def _retire(self,preserve_position=True):
  self._pause_before_retire=None
  self.retiring=True;self._retire_seek_revision=self.seek_revision;self._preserve_retired_position=preserve_position
  self.events.append(('close_requested',self.owner_generation));self.owner.close()
 def close_after_pause(self):
  if not self.owner or self.retiring or self.blocked or not getattr(self.owner,'continuous_sources',False):
   self.close()
   return
  self.revision+=1
  self.intent=replace(self.intent,playing=False)
  self._drain_before_retire(False)
  self.closing=True
  self.poll()
 def close(self):
  self.closing=True
  if self.owner and not self.retiring:self._retire(False)
  self.poll()
 def poll(self):
  if self.blocked and not self.retiring:return
  if self.owner and not self.retiring:
   self.sync_web_intent()
   status=self.owner.snapshot()
   if getattr(status,'ended',False) and getattr(status,'intent_revision',-1)>=self.revision:self.intent=replace(self.intent,playing=False)
   if getattr(status,'error',None):
    # Error fallback is explicit and retains final acknowledged position.
    if not self.allow_fallback:
     self.events.append(('selected_device_failed',status.error))
     self.intent=replace(self.intent,playing=False)
     self.blocked='Selected output failed: '+str(status.error);self._retire(True)
    elif self.owner_mode=='device':
     # User-selected device failed (busy, lost, wrong rate): the bridge rolls the device manager
     # back and re-targets; until then fall back to the shared path, never exclusive.
     self.events.append(('device_fallback',status.error))
     self.intent=replace(self.intent,mode='native-shared' if 'native-shared' in self.factories else 'ordinary',playing=False);self._retire(True)
    elif self.owner_mode in ('native-midi','native-pcm','native-joint','native-audio-click'):
     self.events.append(('native_midi_fallback' if self.owner_mode=='native-midi' else 'native_pcm_fallback',status.error))
     self.intent=replace(self.intent,mode='ordinary',playing=False);self._retire(True)
    elif self.owner_mode in ('raw','native-shared'):
     unsupported='0x88890027' in str(status.error).lower() or 'RAW_MODE_UNSUPPORTED' in str(status.error)
     target='native-shared' if self.owner_mode=='raw' and unsupported and 'native-shared' in self.factories else 'ordinary'
     self.events.append(('raw_fallback' if self.owner_mode=='raw' else 'native_shared_fallback',status.error))
     self.intent=replace(self.intent,mode=target);self._retire(True)
    else:self.blocked='Ordinary output failed: '+status.error;self._retire(False)
  if self._pause_before_retire:
   revision,preserve,seek=self._pause_before_retire
   if status.intent_revision>=revision and not status.playing:
    self._retire(preserve and self.seek_revision==seek)
  if self.retiring:
   status=self.owner.snapshot()
   if not status.closed:return
   if not getattr(status,'output_released',False):
    self.blocked='Output release unconfirmed; replacement playback was NOT started';return
   if self.blocked=='Output release unconfirmed; replacement playback was NOT started':
    # A late release was confirmed by the owner itself: only now may a replacement start.
    self.blocked=None;self.events.append(('late_release_confirmed',self.owner_generation))
   self.events.append(('closed_ack',self.owner_generation))
   if self._preserve_retired_position and self.seek_revision==self._retire_seek_revision and (not self.seek_revision or getattr(status,"position_revision",0)>=self.seek_revision):self.intent=replace(self.intent,position=status.position)
   self.owner=None;self.owner_mode=None;self.retiring=False
  if self.owner is None and not self.closing and not self.blocked:
   self.owner_generation+=1;mode=self.intent.mode
   try:self.owner=self.factories[mode](self.intent,self.owner_generation,self.revision)
   except Exception as exc:
    # Factory contract: raising means it never acquired/runs output.
    if not self.allow_fallback:
     self.events.append(('selected_device_failed',repr(exc)))
     self.intent=replace(self.intent,playing=False)
     self.blocked='Selected output initialization failed: '+repr(exc);return
    if mode in ('native-midi','native-pcm','native-joint','native-audio-click'):
     self.events.append(('native_midi_fallback' if mode=='native-midi' else 'native_pcm_fallback',repr(exc)))
     self.intent=replace(self.intent,mode='ordinary',playing=False)
     # At most one fallback: an ordinary factory exception takes the terminal branch.
     self.poll();return
    self.blocked='Output initialization failed before ownership: '+repr(exc);return
   self.owner_mode=mode;self.events.append(('created',self.owner_generation,mode))
