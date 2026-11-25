"""Isolated MIDI scheduler following native played-position; no independent clock.
Use on the MIDI object's QObject thread. Not wired to application or Handoff.
"""
import math
from time import monotonic
from astra.midi_preview import MidiPreview
class MasterMidiPreview(MidiPreview):
 def __init__(self,*args,**kwargs):
  self.master_position=0.;self.external_clock=False
  super().__init__(*args,**kwargs)
 def play(self):
  if self.playing or not self._events or self.master_position>=self.duration:return
  try:
   self._position=self.master_position;self._synth.open();self._restore(self._position)
   self._available=True;self._started=monotonic();self._publish();self._tick()
  except OSError as exc:self._fail(exc)
 @property
 def position(self):
  if self.external_clock and self.playing:return min(self.duration,self.master_position)
  return super().position
class NativeMidiFollower:
 def __init__(self,midi):
  if not isinstance(midi,MasterMidiPreview):raise TypeError('external-clock MIDI preview required')
  self.m=midi;self.m.external_clock=True;self.m.set_loop(0,0,False)
  self.epoch=None;self.playback_token=None;self.last=None;self.resets=0;self.error=None;self.closed=False
  self.m.error.connect(self._midi_error)
 def _midi_error(self,message):
  self.error=str(message);self.m._timer.stop()
 def step(self,status):
  if self.closed or self.error:return
  pos=float(status.position)
  if not math.isfinite(pos) or pos<0:raise ValueError('invalid native playback position')
  self.m.master_position=pos
  if getattr(status,'error',None) or getattr(status,'closed',False):
   self.m.pause();self.m._timer.stop();self.error=getattr(status,'error',None);return
  active=status.playing and status.ready and not getattr(status,'preparing',False)
  token=getattr(status,'playback_token',None)
  discontinuity=self.epoch!=status.epoch or token!=self.playback_token or (token is None and self.last is not None and pos<self.last-1/48000)
  if not active or pos>=self.m.duration:
   self.m.pause();self.m._timer.stop();self.m._position=min(pos,self.m.duration)
  else:
   if discontinuity or not self.m.playing:
    self.m.pause();self.m.seek(pos);self.m.play();self.resets+=1
   self.m._timer.stop() # Native position is sole authority; no monotonic timer.
   self.m._tick()
  self.epoch=status.epoch;self.playback_token=token;self.last=pos
 def close(self):
  if self.closed:return
  self.closed=True
  self.m._timer.stop();self.m._started=None
  errors=[]
  try:self.m._synth.reset()
  except Exception as exc:errors.append(repr(exc))
  try:self.m._synth.close()
  except Exception as exc:errors.append(repr(exc))
  if errors:raise RuntimeError('MIDI release unconfirmed: '+'; '.join(errors))
