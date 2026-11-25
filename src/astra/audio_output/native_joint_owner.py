"""Opt-in isolated joint owner. Never enabled by production StudioBridge."""
from types import SimpleNamespace
from .raw_transport_owner import RawTransport,NativeRaw
from .separate_midi_clock_worker import NativeClockPair
from .native_midi_follower import NativeMidiFollower,MasterMidiPreview
class NativeJointOwner:
 def __init__(self,cache,duration,intent,generation,revision=0,midi_prepared=None,midi_factory=None,muted=False,backend_factory=None):
  self.generation=generation;self.baseline_revision=revision;self.midi_prepared=midi_prepared
  self.transport=RawTransport(cache,duration,backend_factory=backend_factory or (lambda:NativeRaw(raw=True)),position=intent.position,source=intent.source)
  def factory():
   m=midi_factory() if midi_factory else MasterMidiPreview(available_hint=True)
   if not isinstance(m,MasterMidiPreview):raise TypeError('Native joint requires external-clock MIDI preview')
   path,prepared=midi_prepared;m.load_prepared(path,prepared)
   return NativeMidiFollower(m)
  self.pair=NativeClockPair(self.transport,factory);self.initialization_error=None;self.muted=muted
  try:self.apply(dict(vars(intent)),revision)
  except Exception as exc:self.initialization_error=repr(exc);self.close()
 def apply(self,delta,revision):
  if delta.get('audition','joint')!='joint':raise ValueError('retire joint before audition change')
  d={k:v for k,v in delta.items() if k not in ('mode','audition')}
  if self.muted and 'midi_gain' in d:d['midi_gain']=0.
  return self.pair.apply(d,revision)
 def close(self):self.pair.close()
 def snapshot(self):
  s=self.transport.snapshot();m=self.pair.midi.snapshot();d=dict(vars(s))
  d.update(error=self.initialization_error or self.pair.error or s.error or m.error,closed=s.closed and m.closed,output_released=s.output_released and m.output_released,ready=s.ready and m.ready and m.config_revision>=self.pair.required_config)
  d['position_revision']=max(self.baseline_revision,s.intent_revision) if d['closed'] else 0
  return SimpleNamespace(**d)
