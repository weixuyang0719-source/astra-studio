"""Native PCM master with authored MIDI and/or an optional native click follower.
Source, duration and rate changes require handoff. Click controls do not change
the PCM transport. Close order is MIDI, PCM host, then core/cache.
"""
import queue,threading,time
from concurrent.futures import Future
from .native_pcm_owner import NativePcmOwner,_NativePcmSession,_LIVE
from .native_midi import NativeMidiSession,NativeMidiOwner,_CLICK_GATE
from .native_metronome import metronome_beats

class _JointSession:
    def __init__(self,path,*,prepared,metronome,midi_gain,audio_only=False,pcm_factory=_NativePcmSession,midi_factory=NativeMidiSession,**kwargs):
        self.pcm=pcm_factory(path,**kwargs);self.midi=None;self.prepared=prepared
        self.audio_only=audio_only;self.midi_factory=midi_factory
        self.duration=0;self.rate=kwargs.get('sample_rate',48000)
        self.unreleased_initialization=False;self.last_midi_diagnostics={}
        self._opening=None;self._open_problem=None;self._channel_problem=None
        self._midi_gain=midi_gain;self._applied_gain=None;self._midi_pending=None
        self._click_target=metronome;self._table_tempo=None;self._click_on=False
        self._beat_cache=None;self._map_problem=None

    def _create_midi(self):
        return self.midi_factory(self.prepared,duration=self.duration,master=self.pcm.core)

    def open(self,position,cancel):
        if not self.pcm.open(position,cancel) or cancel.is_set():return False
        self.duration=self.pcm.duration;self.rate=getattr(self.pcm,'rate',self.rate)
        if not self.audio_only:
            try:self.midi=self._create_midi()
            except Exception as e:
                self.unreleased_initialization='release not confirmed' in str(e)
                raise
        return not cancel.is_set()

    def _start_audio_midi(self,cancel):
        # A first WinMM open can block in a driver. Keep the PCM command worker
        # available for pause/seek/close. This task never configures or starts click.
        result=Future()
        def open_midi():
            try:midi=None if cancel.is_set() else self._create_midi()
            except BaseException as e:result.set_exception(e)
            else:result.set_result(midi)
        threading.Thread(target=open_midi,name='Native PCM click open',daemon=True).start()
        self._opening=result

    def _finish_audio_midi(self):
        if self._opening is None:return
        result,self._opening=self._opening,None
        try:self.midi=result.result()
        except Exception as e:
            self.unreleased_initialization='release not confirmed' in str(e)
            self._open_problem='native click initialization: '+str(e)

    def midi_volume(self,value):self._midi_gain=value
    def metronome(self,value):self._click_target=value
    def command(self,*args):return self.pcm.command(*args)

    def service_midi(self,cancel,superseded):
        """Advance control receipts, never the musical clock. One command in flight."""
        if self._opening is not None and self._opening.done():self._finish_audio_midi()
        if self.unreleased_initialization:raise RuntimeError(self._open_problem)
        if self._midi_pending is not None:
            seq,kind,value=self._midi_pending
            receipt=self.midi.receipt(seq)
            if receipt==0:return False
            if receipt!=1:raise RuntimeError('native joint MIDI receipt '+str(receipt))
            self._midi_pending=None
            if kind=='gain':self._applied_gain=value
            elif kind=='gate':self._click_on=value
            else:self._table_tempo=value;self._click_on=True
        if superseded():return False

        if self.midi is not None and self._channel_problem is None:
            diag=self.midi.diagnostics() if hasattr(self.midi,'diagnostics') else {}
            self._channel_problem=diag.get('click_unavailable') or ''
        tempo,enabled=self._click_target
        want=bool(enabled and not (self._open_problem or self._channel_problem))
        self._map_problem=None;beats=()
        if want and tempo!=self._table_tempo:
            # Build against the negotiated master timeline, not an editor or MIDI
            # duration hint. Retain only one prepared table; do no beat work while off.
            if self._beat_cache is None or self._beat_cache[0]!=tempo:
                try:self._beat_cache=(tempo,tuple(metronome_beats(tempo,self.duration)),None)
                except Exception as e:self._beat_cache=(tempo,(),'invalid metronome tempo map: '+str(e))
            _,beats,self._map_problem=self._beat_cache
            want=self._map_problem is None
        # Preparation/open completion is not authority to apply an older intent.
        if superseded():return False
        if self.midi is None:
            if want and self._opening is None:self._start_audio_midi(cancel)
            return not want
        if not want and self._click_on:
            self._midi_pending=(self.midi.command(_CLICK_GATE,0,0),'gate',False)
        elif (want or not self.audio_only) and self._applied_gain!=self._midi_gain:
            self._midi_pending=(self.midi.command(7,self._midi_gain,0),'gain',self._midi_gain)
        elif want and tempo!=self._table_tempo:
            self._midi_pending=(self.midi.set_clicks(beats,True),'table',tempo)
        elif want and not self._click_on:
            self._midi_pending=(self.midi.command(_CLICK_GATE,1,0),'gate',True)
        else:return True
        return False

    def _diagnostics(self):
        d=self.midi.diagnostics() if self.midi is not None and hasattr(self.midi,'diagnostics') else {}
        d.setdefault('click_enabled',0)
        d['click_requested']=int(self._click_target[1])
        problem=self._open_problem or self._channel_problem or self._map_problem
        if problem:d['click_unavailable']=problem
        return d

    def snapshot(self):
        s=self.pcm.snapshot();observed=None
        if self.midi is not None:
            m=self.midi.snapshot()
            if m.error:raise RuntimeError('native joint MIDI failure '+str(m.error))
            observed=self.midi.master_snapshot()
        needs_clock=not self.audio_only or self._click_on
        s['midi_ready']=not needs_clock or bool(observed is not None and observed.epoch==s['audible_epoch'] and bool(observed.playing)==bool(s['audible_active']))
        s['midi_epoch']=observed.epoch if observed is not None else None
        s['midi_device']=self.midi.device if self.midi is not None else ''
        s['midi_diagnostics']=self.last_midi_diagnostics=self._diagnostics()
        return s

    def close(self,timeout=5):
        # Silence PCM before waiting for a possibly still-opening follower. Keep
        # the master alive, and do not acknowledge release until that task resolves.
        if getattr(self.pcm,'core',None):
            try:self.pcm.command(1,0,0)
            except Exception:pass
        return self.close_follower() and self.pcm.close(timeout)

    def close_follower(self):
        self._finish_audio_midi()
        if self.unreleased_initialization:return False
        if self.midi is not None:
            released=self.midi.close()
            try:self.last_midi_diagnostics=self._diagnostics()
            except Exception:pass
            if not released:return False
            self.midi=None
        return True

    @classmethod
    def follow_review(cls,pcm,rate):
        owner=cls('',prepared=([],[],0,{}),metronome=(None,False),midi_gain=1.,
                  audio_only=True,pcm_factory=lambda *a,**k:pcm,sample_rate=rate)
        owner.duration=pcm.duration
        return owner

class NativePcmMidiOwner(NativePcmOwner):
    inplace_metronome=True

    def __init__(self,paths,midi_prepared,intent,generation,revision=0,*,pcm_session_factory=None,midi_session_factory=None,**kwargs):
        self.midi_prepared=midi_prepared;self._audition=intent.audition
        audio_only=intent.audition=='audio'
        prepared=([],[],0,{}) if audio_only else midi_prepared[1]
        kwargs['timeline_duration']=max(kwargs.get('timeline_duration',0),float(prepared[2]))
        def factory(path,**options):
            return _JointSession(path,prepared=prepared,metronome=intent.metronome,midi_gain=0 if kwargs.get('muted') else intent.midi_gain,
                audio_only=audio_only,pcm_factory=pcm_session_factory or _NativePcmSession,midi_factory=midi_session_factory or NativeMidiSession,**options)
        super().__init__(paths,intent,generation,revision,session_factory=factory,**kwargs)
    def _validate(self,d):
        if d.get('audition',self._audition)!=self._audition or self._audition not in ('joint','audio'):raise ValueError('native joint audition change requires handoff')
        NativeMidiOwner._validate(self,{**d,'audition':'midi'})
        if 'source' in d and (type(d['source'])is not int or not 0<=d['source']<len(self.paths)):raise ValueError('invalid source')
    def _update(self,s):
        super()._update(s)
        self._state.update(midi_device=s.get('midi_device',''),midi_epoch=s.get('midi_epoch'),midi_diagnostics=s.get('midi_diagnostics',{}))
    def _run(self):
        session=None
        try:
            session=self._factory(self.paths[self._source],**self._kwargs)
            with self._lock:self._session=session
            if not session.open(self._intent.get('position',0),self._closing):return
            self._rate=session.rate
            def superseded():return self._closing.is_set() or not self._queue.empty()
            while not self._closing.is_set():
                try:d,rev=self._queue.get(timeout=.02)
                except queue.Empty:
                    # Reap a late optional open even if the user turned click off.
                    session.service_midi(self._closing,superseded)
                    s=session.snapshot()
                    with self._lock:
                        if self._state['ready']:self._update(s)
                    continue
                def drain(fields,revision,seek_revision):
                    for _ in range(256):
                        try:newer,newrev=self._queue.get_nowait()
                        except queue.Empty:break
                        fields.update(newer);revision=newrev
                        if 'position' in newer:seek_revision=newrev
                    return fields,revision,seek_revision
                held_play=False
                def submit(fields):
                    nonlocal held_play
                    commands=[]
                    if 'midi_gain' in fields:session.midi_volume(0 if self._muted else fields['midi_gain'])
                    if 'metronome' in fields:session.metronome(fields['metronome'])
                    if 'gain' in fields:commands.append((3,0 if self._muted else fields['gain'],0))
                    if 'loop' in fields:
                        a,b,on=fields['loop']
                        if on and max(a,b)>session.duration:raise ValueError('loop outside joint timeline')
                        commands.append((4,round(a*self._rate),round(b*self._rate)) if on else (5,0,0))
                    if 'position' in fields:commands.append((2,round(fields['position']*self._rate),0))
                    if 'playing' in fields:
                        # Only an explicit Play is held for initial gain/table receipts.
                        # Pause, seek and loop commands can go out during that wait.
                        held_play=fields['playing']
                        if not held_play:commands.append((1,0,0))
                    last=None
                    for cmd in commands:
                        if self._closing.is_set():break
                        last=session.command(*cmd)
                    return last
                d,rev,position_rev=drain(d,rev,rev if 'position' in d else None)
                seq=submit(d);deadline=time.monotonic()+5;seek_epoch=None;pause_boundary=None
                while not self._closing.is_set():
                    # Do not make newer pause/seek wait for an older DAC/MIDI receipt.
                    try:newer,newrev=self._queue.get_nowait()
                    except queue.Empty:pass
                    else:
                        newer,newrev,newposrev=drain(newer,newrev,newrev if 'position' in newer else None)
                        d.update(newer);rev=newrev
                        if newposrev is not None:position_rev=newposrev;seek_epoch=None
                        if 'loop' in newer:seek_epoch=None
                        if any(k in newer for k in ('playing','position','loop')):pause_boundary=None
                        next_seq=submit(newer)
                        if next_seq is not None:seq=next_seq
                        deadline=time.monotonic()+5
                        continue
                    controls_ready=session.service_midi(self._closing,superseded)
                    s=session.snapshot()
                    with self._lock:
                        if self._state['ready']:self._update(s)
                    if superseded():continue
                    if held_play and controls_ready:
                        seq=session.command(1,1,0);held_play=False
                        deadline=time.monotonic()+5
                        continue
                    applied=seq is None or s['applied_commands']>=seq
                    # Seek receipt is not audible receipt. Wait until native MIDI
                    # observed the DAC epoch, not just the render-thread command.
                    if applied and 'position' in d and seek_epoch is None:seek_epoch=s['rendered_epoch']
                    epoch_ready='position' not in d or (seek_epoch is not None and s['audible_epoch']>=seek_epoch)
                    state_ready=True
                    if 'playing' in d:
                        if d['playing']:
                            state_ready=bool(s['audible_active']) or (s['audible_position']>=s['duration'] and not s.get('rendered_active',False))
                        else:
                            # Matching MIDI inactivity can describe an old DAC silent
                            # gap too; require the actual final pause source boundary.
                            if applied and pause_boundary is None and not s.get('rendered_active',False):
                                pause_boundary=(s.get('rendered_epoch',0),s.get('rendered_position',s['audible_position']))
                            state_ready=(pause_boundary is not None and not s['audible_active']
                                and s.get('audible_epoch',0)>=pause_boundary[0]
                                and abs(s['audible_position']-pause_boundary[1])<=1/self._rate)
                    if not held_play and controls_ready and applied and epoch_ready and state_ready and s['midi_ready']:break
                    if time.monotonic()>deadline:raise TimeoutError('joint DAC/MIDI acknowledgment pending')
                    self._closing.wait(.002)
                if self._closing.is_set():break
                with self._lock:
                    self._intent.update(d);self._state.update(ready=True,intent_revision=rev)
                    if 'position' in d:
                        self._state['position_revision']=position_rev
                        if self._pending_target and self._pending_target[1]==position_rev:self._pending_target=None
                    self._update(s)
        except Exception as e:
            with self._lock:self._state['error']=str(e)
        finally:
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
                with self._lock:self._state['error']='native joint close: '+str(e)
            with self._lock:
                self._failed_session=None if released else session
                if session:self._state['midi_diagnostics']=session.last_midi_diagnostics
                if self._pending_target:self._state['position'],self._state['position_revision']=self._pending_target
                self._state.update(closed=True,output_released=released,playing=False,preparing=False,ready=False)
                if not released:self._state['error']='native joint output release not confirmed'
            if released:_LIVE.pop(id(self),None)
