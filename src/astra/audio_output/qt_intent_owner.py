"""Experimental thread-owned ordinary Qt output for the same handoff protocol."""
import threading,copy,math
_LIVE={}
def _cycle_target(value, loop):
 a,z,on=loop
 return a if on and ((a<z and value>=z) or (a>z and z<=value<a)) else value
from types import SimpleNamespace
from PySide6.QtCore import QObject,QThread,Signal,Slot,QUrl,Qt
from PySide6.QtMultimedia import QMediaPlayer,QAudioOutput
import shiboken6
class Commands(QObject):
 apply=Signal(object,int)
 close=Signal()
class Worker(QObject):
 def __init__(self,owner):super().__init__();self.o=owner;self.p=None;self.a=None;self.m=None;self.pending=None;self.intent=None;self.revision=0;self.closing=False
 @Slot()
 def start(self):
  if self.o.closing:self.close();return
  try:
   self.p=QMediaPlayer();self.a=QAudioOutput();self.p.setAudioOutput(self.a)
   self.p.mediaStatusChanged.connect(self.loaded);self.p.seekableChanged.connect(lambda _:self.loaded(self.p.mediaStatus()));self.p.durationChanged.connect(lambda _:self.loaded(self.p.mediaStatus()));self.p.positionChanged.connect(self.position);self.p.playbackStateChanged.connect(self.state);self.p.errorOccurred.connect(self.failure)
   if self.o.midi_prepared is not None:
    from astra.midi_preview import MidiPreview
    self.m= self.o.midi_factory() if self.o.midi_factory else MidiPreview()
    path,prepared=self.o.midi_prepared;self.m.load_prepared(path,prepared);self._base_midi_duration=self.m.duration
    self.m.changed.connect(self.midi_state);self.m.error.connect(lambda msg:self.failure(None,msg))
   self.apply(dict(vars(self.o.initial)),self.o.initial_revision,False)
  except Exception as e:self.failure(None,str(e))
 @Slot(object,int)
 def apply(self,delta,revision,queued=True):
  if queued:
   with self.o.lock:self.o.queued=max(0,self.o.queued-1)
  if self.closing or self.o.closing:return
  from dataclasses import replace
  try:
   if self.intent is not None and 'source' in delta and 'position' not in delta:
    delta=dict(delta,position=self.pending[0] if self.pending else self.p.position()/1000)
   self.intent=replace(self.intent or self.o.initial,**delta);self.revision=revision
   i=self.intent
   if i.audition not in ('audio','midi','joint'):raise ValueError('invalid audition')
   if i.audition!='audio' and self.m is None:raise ValueError('MIDI not prepared')
   if self.m is not None:
    duration=max(self._base_midi_duration,i.midi_duration)
    if duration!=self.m.duration:
     old_position=self.m.position
     if old_position>=duration and i.audition=='midi':
      self.m.pause();self.m._duration=duration;self.m.seek(duration)
      self.intent=i=replace(i,playing=False);self.o.update(ended=True,playing=False)
     else:self.m._duration=duration
     self.m._metronome_signature=None
    self.m.set_metronome(*i.metronome)
    self.m.set_volume(0 if self.o.muted else round(i.midi_gain*100));self.m.set_rate(i.rate);self.m.set_loop(*i.loop)
    if 'position' in delta:self.m.seek(i.position)
    if i.audition=='audio' or not i.playing:self.m.pause()
   if i.audition=='midi':
    self.pending=None;self.p.stop()
    if i.playing:self.m.play()
    self.o.update(intent_revision=revision,position_revision=revision,ready=True);return
   path=self.o.paths[i.source];url=QUrl.fromLocalFile(path)
   self.a.setVolume(i.gain);self.a.setMuted(self.o.muted);self.p.setPlaybackRate(i.rate)
   if self.p.source()!=url:
    self.pending=(i.position,i.playing,revision);self.p.setSource(url)
   elif self.pending is not None or 'position' in delta:
    self.pending=(i.position,i.playing,revision)
   else:
    if 'position' in delta:self.p.setPosition(round(i.position*1000))
    if i.playing:
     current=self.p.position()/1000;target=_cycle_target(current,i.loop)
     if target!=current:
      self.p.setPosition(round(target*1000))
      if i.audition=='joint':self.m.seek(target)
     self.p.play()
     if i.audition=='joint':self.m.play()
    else:self.p.pause()
   self.o.update(intent_revision=revision,position_revision=revision,ready=True)
   self.loaded(self.p.mediaStatus())
  except Exception as e:self.failure(None,str(e))
 @Slot(object)
 def loaded(self,status):
  if self.closing or self.o.closing:return
  if status==QMediaPlayer.MediaStatus.EndOfMedia:
   if self.m is not None and self.intent and self.intent.audition=='joint':self.m.pause()
   self.o.update(ended=True,playing=False)
  if not self.pending:return
  if status not in (QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia):return
  pos,playing,rev=self.pending
  if pos and (not self.p.isSeekable() or self.p.duration()<=0):return
  if rev!=self.revision:return
  self.pending=None
  if not playing:self.p.pause()
  else:pos=_cycle_target(pos,self.intent.loop)
  self.p.setPosition(round(pos*1000))
  if playing:
   self.p.play()
   if self.intent.audition=='joint':self.m.seek(pos);self.m.play()
 @Slot(str)
 def midi_state(self,payload):
  if not self.closing and self.intent and self.intent.audition=='midi':
   import json
   s=json.loads(payload);self.o.update(position=s['position'],playing=s['playing'],ended=not s['playing'] and s['position']>=s['duration'])
 @Slot(int)
 def position(self,value):
  if self.closing or (self.intent and self.intent.audition=='midi'):return
  i=self.intent
  if i and i.playing and i.loop[2]:
   a,z=i.loop[:2];seconds=value/1000
   if (a<z and seconds>=z) or (a>z and z<=seconds<a):
    self.p.setPosition(round(a*1000))
    if i.audition=='joint' and self.m is not None:self.m.seek(a)
    return
  self.o.update(position=value/1000)
 @Slot(object)
 def state(self,state):
  if self.closing or (self.intent and self.intent.audition=='midi'):return
  self.o.update(playing=state==QMediaPlayer.PlaybackState.PlayingState,ended=self.p.mediaStatus()==QMediaPlayer.MediaStatus.EndOfMedia)
 @Slot(object,str)
 def failure(self,error,message):
  self.o.update(error=message);self.o.close()
 @Slot()
 def close(self):
  if self.closing:return
  self.closing=True;errors=[]
  if self.m is not None:
   if self.intent and self.intent.audition=='midi':self.o.update(position=self.m.position)
   try:
    self.m._timer.stop();self.m._started=None;self.m._synth.close()
   except Exception as e:errors.append('MIDI release: '+repr(e))
   try:shiboken6.delete(self.m);self.m=None
   except Exception as e:errors.append('MIDI object: '+repr(e))
  if self.p is not None:
   try:
    if not self.intent or self.intent.audition!='midi':self.o.update(position=self.pending[0] if self.pending else self.p.position()/1000)
    self.p.stop();shiboken6.delete(self.p);self.p=None
   except Exception as e:errors.append('Qt player: '+repr(e))
  if self.a is not None:
   try:shiboken6.delete(self.a);self.a=None
   except Exception as e:errors.append('Qt output: '+repr(e))
  if errors:self.o.update(error='; '.join(errors))
  self.o.update(output_released=not errors,playing=False);QThread.currentThread().quit()
class QtIntentOwner:
 def __init__(self,paths,intent,generation,revision=0,muted=False,midi_prepared=None,midi_factory=None):
  self.midi_prepared=midi_prepared;self.midi_factory=midi_factory
  self.paths=[str(p) for p in paths];self.initial=intent;self.initial_revision=revision;self.muted=muted;self.generation=generation;self.lock=threading.Lock();self.closing=False;self.queued=0
  self.status=dict(closed=False,output_released=False,playing=False,ended=False,position=intent.position,intent_revision=revision,position_revision=revision,error=None,ready=False)
  self._validate(dict(vars(intent)))
  self.thread=QThread();self.worker=Worker(self);self.worker.moveToThread(self.thread);self.commands=Commands()
  self.commands.apply.connect(self.worker.apply,Qt.ConnectionType.QueuedConnection);self.commands.close.connect(self.worker.close,Qt.ConnectionType.QueuedConnection)
  self.thread.started.connect(self.worker.start);self.thread.finished.connect(self.worker.deleteLater);_LIVE[id(self)]=self;self.thread.finished.connect(self._finished);self.thread.start()
 def _finished(self):
  self.update(closed=True,thread_finished=True);_LIVE.pop(id(self),None)
 def _validate(self,delta):
  if set(delta)-{'mode','audition','midi_gain','position','source','gain','loop','playing','rate','metronome','midi_duration'}:raise ValueError('unknown field')
  if 'position' in delta and (not math.isfinite(delta['position']) or delta['position']<0):raise ValueError('invalid position')
  if 'source' in delta and (type(delta['source'])is not int or not 0<=delta['source']<len(self.paths)):raise ValueError('invalid source')
  if 'midi_gain' in delta and not 0<=delta['midi_gain']<=1:raise ValueError('invalid MIDI gain')
  if 'gain' in delta and not 0<=delta['gain']<=1:raise ValueError('invalid gain')
  if 'rate' in delta and not .25<=delta['rate']<=2:raise ValueError('invalid rate')
  if 'playing' in delta and type(delta['playing'])is not bool:raise ValueError('invalid playing')
  if 'audition' in delta and (delta['audition']not in ('audio','midi','joint') or (delta['audition']!='audio' and self.midi_prepared is None)):raise ValueError('invalid audition')
  if 'midi_duration' in delta and (not math.isfinite(delta['midi_duration']) or delta['midi_duration']<0):raise ValueError('invalid MIDI duration')
  if 'metronome' in delta:
   tempo,enabled=delta['metronome']
   if type(enabled)is not bool or (enabled and not isinstance(tempo,dict)):raise ValueError('invalid metronome intent')
  if 'loop' in delta:
   a,z,on=delta['loop']
   if type(on)is not bool or not math.isfinite(a) or not math.isfinite(z) or min(a,z)<0:raise ValueError('invalid loop')
 def update(self,**delta):
  if 'position' in delta:
   import time;delta['sampled_at']=time.perf_counter()
  with self.lock:self.status.update(delta)
 def snapshot(self):
  with self.lock:return SimpleNamespace(**self.status)
 def apply(self,delta,revision):
  payload=copy.deepcopy(delta);self._validate(payload)
  with self.lock:
   if self.closing:raise RuntimeError('Qt owner closing')
   if self.queued>=256:raise BufferError('Qt command queue full')
   self.queued+=1
  self.commands.apply.emit(payload,revision)
 def close(self):
  with self.lock:
   if self.closing:return
   self.closing=True
  self.commands.close.emit()

