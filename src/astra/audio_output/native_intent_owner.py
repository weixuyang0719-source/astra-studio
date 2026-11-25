"""Handoff owner for the legacy native audio transports."""
from types import SimpleNamespace
from .raw_transport_owner import RawTransport,NativeRaw
class NativeIntentOwner:
 inplace_metronome=False
 def __init__(self,cache,duration,intent,generation,revision=0,backend_factory=None,observer_factory=None,pause_guard=False):
  self.generation=generation;self.baseline_revision=revision
  self.inplace_metronome=getattr(intent,'audition','audio')=='audio' and observer_factory is None
  factory=backend_factory or (lambda:NativeRaw(raw=intent.mode=='raw'))
  transport_type=RawTransport
  if pause_guard is True and intent.mode in ('raw','native-shared','device') and intent.audition=='audio' and intent.rate==1 and observer_factory is None:
   from .raw_pause_guard import PauseGuardTransport
   transport_type=PauseGuardTransport
  self.transport=transport_type(cache,duration,backend_factory=factory,position=intent.position,source=intent.source,observer_factory=observer_factory)
  initial=dict(source=intent.source,gain=intent.gain,loop=intent.loop,position=intent.position,playing=intent.playing,rate=intent.rate)
  if self.inplace_metronome:
   initial.update(metronome=getattr(intent,'metronome',(None,False)),midi_gain=getattr(intent,'midi_gain',1.))
  try:self.transport.apply(initial,revision)
  except Exception as exc:
   # Keep owner tracked even if initialization intent fails; caller polls release.
   self.transport.close();self.initialization_error='initial intent rejected: '+str(exc)
  else:self.initialization_error=None
 def apply(self,delta,revision):
  return self.transport.apply({k:v for k,v in delta.items() if k!='midi_duration' and (k!='metronome' or self.inplace_metronome)},revision)
 def close(self):return self.transport.close()
 def snapshot(self):
  s=self.transport.snapshot();d=dict(vars(s))
  d['error']=s.error or self.initialization_error
  # Only closed state carries a preserved final target contract, never an audible claim.
  d['position_revision']=max(self.baseline_revision,s.intent_revision) if s.closed else 0
  return SimpleNamespace(**d)
