"""Native-only MIDI timing; Python controls initialization/commands/ownership.

Standalone MIDI or native master-clock follower; no Python realtime callback.
Metronome events are prepared off-thread and timed by the same native scheduler.
"""
import ctypes as C
import copy
import math
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

_LIVE = {}
_CLICK_GATE = 9

class Event(C.Structure):
    _fields_ = [('seconds', C.c_double), ('packed', C.c_uint32), ('reserved', C.c_uint32)]
class Click(C.Structure):
    _fields_ = [('seconds',C.c_double),('accent',C.c_uint32),('reserved',C.c_uint32)]
class MemoryPlan(C.Structure):
    _fields_ = [('size',C.c_uint32),('version',C.c_uint32)] + [(x,C.c_uint64) for x in ('gate','score','locator')] + [(x,C.c_double) for x in ('position','duration','rate','gain','left','right')] + [('flags',C.c_uint32),('reserved',C.c_uint32)]
class ClickStatus(C.Structure):
    _fields_ = [('size',C.c_uint32),('version',C.c_uint32)] + [(x,C.c_uint64) for x in ('note_ons','note_offs','late_skipped')] + [('beats',C.c_uint32),('enabled',C.c_uint32),('channel',C.c_int32),('gain_cc7',C.c_uint32)]
class Note(C.Structure):
    _fields_ = [(x, C.c_double) for x in ('start','end','sounding_end')] + [(x,C.c_uint32) for x in ('channel','pitch','velocity','reserved')]
class Status(C.Structure):
    _fields_ = [('size',C.c_uint32),('version',C.c_uint32),('seconds',C.c_double)] + [(x,C.c_uint64) for x in ('accepted','applied','sent')] + [(x,C.c_uint32) for x in ('playing','closed')] + [('error',C.c_int32),('reserved',C.c_int32)]
class MasterClock(C.Structure):
    _fields_=[("size",C.c_uint32),("version",C.c_uint32),("seconds",C.c_double),("epoch",C.c_uint64),("playing",C.c_uint32),("reserved",C.c_uint32)]
class DriverDiagnostics(C.Structure):
    _fields_=[('size',C.c_uint32),('version',C.c_uint32)]+[(p+'_'+f,C.c_uint64) for p in ('send','reset') for f in ('calls','completed','errors','total_ns','max_ns')]
class DeviceStatus(C.Structure):
    _fields_ = [(x,C.c_uint32) for x in ('size','version','open','borrowers')] + [('error',C.c_int32),('reserved',C.c_int32),('name',C.c_wchar*128)]

def pack_prepared(prepared):
    events, notes, duration, metadata = prepared
    channels = {n.channel for n in notes}
    packed = []
    for at, msg in events:
        if getattr(msg,'channel',None) not in channels: continue
        data = msg.bytes()
        if not 1 <= len(data) <= 3 or not 0x80 <= data[0] <= 0xef:
            raise ValueError('unsupported MIDI short message')
        packed.append(Event(float(at),sum(b << (8*i) for i,b in enumerate(data)),0))
    return (Event*len(packed))(*packed), (Note*len(notes))(*(Note(n.start,n.end,n.sounding_end,n.channel,n.pitch,n.velocity,0) for n in notes)), float(duration)

class NativeMidiSession:
    """Synchronous control binding. Call from the owner control thread, not Qt."""
    def __init__(self, prepared=None, duration=None, directory=None, device=None, master=None, *, memory=False, silent=False):
        from .native_assets import native_library
        resolve = (lambda name: Path(directory)/name) if directory is not None else native_library
        self.s = C.CDLL(str(resolve('astra_midi_scheduler.dll')))
        self.handle = self.sender = None; self._panic_required=False; self.master=master; self._last_diagnostics={}
        def bind(lib,name,args,result):
            fn=getattr(lib,name); fn.argtypes=args; fn.restype=result
        P=C.c_void_p; I=C.c_int; U=C.c_uint32; D=C.c_double
        bind(self.s,'astra_midi_abi_version',[],U)
        if self.s.astra_midi_abi_version()!=1: raise RuntimeError('native MIDI ABI mismatch')
        try:
            bind(self.s,'astra_midi_set_clicks',[P,C.POINTER(Click),C.c_uint64,U],C.c_int64)
            bind(self.s,'astra_midi_metronome_status',[P,C.POINTER(ClickStatus)],I)
        except AttributeError as e:
            raise RuntimeError('native MIDI DLL lacks in-place metronome support; rebuild astra_midi_scheduler.dll') from e
        bind(self.s,'astra_midi_create_owned',[C.POINTER(Event),C.c_uint64,C.POINTER(Note),C.c_uint64,D,P,P,P,P,P,P,P,P],P)
        bind(self.s,'astra_midi_command',[P,U,D,D],C.c_int64)
        bind(self.s,'astra_midi_snapshot',[P,C.POINTER(Status)],I)
        bind(self.s,'astra_midi_master_snapshot',[P,C.POINTER(MasterClock)],I)
        bind(self.s,'astra_midi_receipt',[P,C.c_uint64],I)
        bind(self.s,'astra_midi_close',[P],None)
        bind(self.s,'astra_midi_wait',[P,U],I)
        bind(self.s,'astra_midi_destroy',[P],I)
        if memory:
            if master is not None:raise ValueError('memory MIDI transport requires a standalone clock')
            try:
                bind(self.s,'astra_midi_create_memory_owned',[P,P,P,P],P)
                bind(self.s,'astra_midi_memory_fence',[P,C.c_uint64],I)
                bind(self.s,'astra_midi_memory_submit',[P,C.POINTER(MemoryPlan),C.POINTER(Event),C.c_uint64,C.POINTER(Click),C.c_uint64],C.c_int64)
            except AttributeError as e:
                raise RuntimeError('native MIDI DLL lacks memory transport; rebuild astra_midi_scheduler.dll') from e
            self.duration=0.
        else:
            events,notes,base=pack_prepared(prepared)
            self.duration=max(base,float(duration or 0))
        if silent:
            if master is not None:
                raise ValueError('silent MIDI requires its own existing scheduler clock')
            bind(self.s,'astra_midi_create_silent',[C.POINTER(Event),C.c_uint64,C.POINTER(Note),C.c_uint64,D],P)
            bind(self.s,'astra_midi_create_memory_silent',[],P)
            self.device='No instrument (silent)'
            self.handle=(self.s.astra_midi_create_memory_silent() if memory else
                         self.s.astra_midi_create_silent(events,len(events),notes,len(notes),self.duration))
            if not self.handle:raise RuntimeError('silent MIDI scheduler creation failed')
            return
        self.w = C.CDLL(str(resolve('astra_midi_winmm.dll')))
        bind(self.w,'astra_winmm_create',[],P)
        bind(self.w,'astra_winmm_open',[P,C.c_wchar_p],I)
        bind(self.w,'astra_winmm_snapshot',[P,C.POINTER(DeviceStatus)],I)
        bind(self.w,'astra_winmm_diagnostics',[P,C.POINTER(DriverDiagnostics)],I)
        bind(self.w,'astra_winmm_close',[P],I)
        bind(self.w,'astra_winmm_panic',[P],I)
        bind(self.w,'astra_winmm_destroy',[P],I)
        self.sender=self.w.astra_winmm_create()
        if not self.sender: raise RuntimeError('WinMM sender allocation failed')
        try:
            code=self.w.astra_winmm_open(self.sender,device)
            if code: raise RuntimeError(f'WinMM open failed ({code})')
            ds=DeviceStatus(size=C.sizeof(DeviceStatus))
            if self.w.astra_winmm_snapshot(self.sender,C.byref(ds)): raise RuntimeError('WinMM status failed')
            self.device=ds.name
            def create(clock=None,user=None,retain=None,release=None):
                return self.s.astra_midi_create_owned(events,len(events),notes,len(notes),self.duration,clock,user,C.cast(self.w.astra_winmm_send,P),self.sender,retain,release,C.cast(self.w.astra_winmm_retain,P),C.cast(self.w.astra_winmm_release,P))
            if memory:
                self.handle=self.s.astra_midi_create_memory_owned(C.cast(self.w.astra_winmm_send,P),self.sender,C.cast(self.w.astra_winmm_retain,P),C.cast(self.w.astra_winmm_release,P))
            elif master is None:self.handle=create()
            else:
                # Lock across native owned-create retain; never race Python core.close.
                with master._lock:
                    if not master.handle:raise RuntimeError('native MIDI master already closed')
                    self.handle=create(C.cast(master.lib.astra_core_midi_clock,P),master.handle,C.cast(master.lib.astra_core_retain,P),C.cast(master.lib.astra_core_release,P))
            if not self.handle: raise RuntimeError('native MIDI scheduler creation failed')
        except Exception:
            # A lazy follower may treat a clean initialization failure as optional.
            # Do not report it as clean if cleanup itself failed or raised.
            try:released=self.close()
            except Exception:released=False
            if not released:
                _LIVE[id(self)]=self
                raise RuntimeError('initialization failed; output release not confirmed')
            raise
    def memory_fence(self,gate):
        # Caller serializes this one cross-thread operation against detach.
        if self.s.astra_midi_memory_fence(self.handle,gate):
            raise RuntimeError('native memory MIDI fence rejected')
    def memory_submit(self,plan,events,clicks):
        seq=self.s.astra_midi_memory_submit(self.handle,C.byref(plan),events,0 if events is None else len(events),clicks,0 if clicks is None else len(clicks))
        if seq<=0:raise RuntimeError(f'native memory MIDI plan rejected ({seq})')
        return seq
    def command(self,kind,a=0,b=0):
        seq=self.s.astra_midi_command(self.handle,kind,a,b)
        if seq<=0: raise RuntimeError(f'native MIDI command rejected ({seq})')
        return seq
    def set_clicks(self,beats,enabled):
        """Queue a copied beat table and gate; wait for its receipt on the control thread."""
        count=len(beats)
        if count>100000:raise ValueError('metronome event budget exceeded')
        data=(Click*count)()
        for i,(at,accent) in enumerate(beats):data[i]=Click(at,int(accent),0)
        seq=self.s.astra_midi_set_clicks(self.handle,data,count,int(enabled))
        if seq<=0:raise RuntimeError(f'native MIDI click timeline rejected ({seq})')
        return seq
    def receipt(self,seq): return self.s.astra_midi_receipt(self.handle,seq)
    def snapshot(self):
        value=Status(size=C.sizeof(Status))
        if self.s.astra_midi_snapshot(self.handle,C.byref(value)): raise RuntimeError('native MIDI snapshot failed')
        return value
    def diagnostics(self):
        if not self.sender:return dict(getattr(self,"_last_diagnostics",{}))
        value=DriverDiagnostics(size=C.sizeof(DriverDiagnostics),version=1)
        if self.w.astra_winmm_diagnostics(self.sender,C.byref(value)):raise RuntimeError('native MIDI diagnostics failed')
        result={key:getattr(value,key) for key,_ in value._fields_ if key not in ('size','version')}
        if self.handle:
            click=ClickStatus(size=C.sizeof(ClickStatus),version=1)
            if self.s.astra_midi_metronome_status(self.handle,C.byref(click)) or click.version!=1:
                raise RuntimeError('native MIDI metronome status failed')
            result.update(('click_'+key,getattr(click,key)) for key,_ in click._fields_ if key not in ('size','version'))
            if click.channel<0:result['click_unavailable']='no unused MIDI channel'
        else:
            result.update((key,v) for key,v in self._last_diagnostics.items() if key.startswith('click_'))
        return result
    def master_snapshot(self):
        value=MasterClock(size=C.sizeof(MasterClock),version=1)
        result=self.s.astra_midi_master_snapshot(self.handle,C.byref(value))
        if result!=0:return None
        return value
    def close(self):
        if self.handle:
            self.s.astra_midi_close(self.handle)
            if not self.s.astra_midi_wait(self.handle,1000): return False
            status=self.snapshot()
            self._panic_required = self._panic_required or bool(status.error)
            try:self._last_diagnostics=self.diagnostics()
            except Exception:pass # diagnostics must not prevent release
            if not self.s.astra_midi_destroy(self.handle): return False
            self.handle=None
        if self.sender:
            if self._panic_required:
                if self.w.astra_winmm_panic(self.sender): return False
                self._panic_required=False
            if self.w.astra_winmm_close(self.sender): return False
            try:self._last_diagnostics=self.diagnostics()
            except Exception:pass
            if not self.w.astra_winmm_destroy(self.sender): return False
            self.sender=None
        return True

class NativeMidiOwner:
    """Handoff owner, asynchronous open/close, completion only after native receipts."""
    def __init__(self,midi_prepared,intent,generation,revision=0,session_factory=None,*,muted=False):
        self.generation=generation; self.baseline_revision=revision; self.midi_prepared=midi_prepared; self._muted=bool(muted)
        self._intent=copy.deepcopy(vars(intent)); self._validate(self._intent)
        self._prepared=midi_prepared[1]; self._factory=session_factory or NativeMidiSession
        self._lock=threading.RLock(); self._queue=queue.Queue(256); self._closing=threading.Event()
        self._session=None; self._pending_target=None
        self._state=dict(closed=False,output_released=False,playing=False,ended=False,position=intent.position,intent_revision=revision,position_revision=revision,error=None,ready=False)
        self._queue.put((self._intent.copy(),revision)); _LIVE[id(self)]=self
        self._thread=threading.Thread(target=self._run,name='Native MIDI control',daemon=True); self._thread.start()
    def _validate(self,d):
        if set(d)-{'mode','audition','midi_gain','position','source','gain','loop','playing','rate','metronome','midi_duration'}: raise ValueError('unknown MIDI intent field')
        if d.get('audition','midi')!='midi': raise ValueError('native MIDI owner supports MIDI only')
        for k in ('position','gain','midi_gain','rate','midi_duration'):
            if k in d and (not math.isfinite(d[k]) or d[k]<0): raise ValueError('invalid '+k)
        for k in ('gain','midi_gain'):
            if k in d and d[k]>1: raise ValueError('invalid '+k)
        if 'rate' in d and not .25<=d['rate']<=2: raise ValueError('invalid rate')
        if 'playing' in d and type(d['playing'])is not bool: raise ValueError('invalid playing')
        if 'loop' in d:
            a,b,on=d['loop']
            if type(on)is not bool or not all(math.isfinite(v) and v>=0 for v in (a,b)): raise ValueError('invalid loop')
        if 'metronome' in d:
            tempo,on=d['metronome']
            if type(on)is not bool or (tempo is not None and not isinstance(tempo,dict)) or (on and not tempo):
                raise ValueError('invalid metronome')
            # TempoMap validation and beat generation belong to the control worker.
    def apply(self,delta,revision):
        delta=copy.deepcopy(delta); self._validate(delta)
        with self._lock:
            if self._closing.is_set(): raise RuntimeError('native MIDI owner closing')
            if 'midi_duration' in delta and delta['midi_duration']!=self._intent.get('midi_duration',0): raise ValueError('MIDI duration change requires owner handoff')
            # Only replace the unconsumed tail; never cross a non-seek barrier.
            seek = 'position' in delta and set(delta) <= {'position','mode'}
            replaced = False
            if seek:
                with self._queue.mutex:
                    if self._queue.queue:
                        old, _ = self._queue.queue[-1]
                        if ('position' in old and set(old) <= {'position','mode'}
                                and ('mode' not in old or 'mode' not in delta or old['mode']==delta['mode'])):
                            self._queue.queue[-1] = ({**old, **delta},revision)
                            replaced = True
            if not replaced: self._queue.put_nowait((delta,revision))
            if 'position' in delta: self._pending_target=(delta['position'],revision)
    def close(self):
        with self._lock: self._closing.set()
    def snapshot(self):
        with self._lock:
            if self._session and self._session.handle:
                s=self._session.snapshot()
                self._state.update(position=s.seconds,playing=bool(s.playing),ended=not s.playing and s.seconds>=self._session.duration,sampled_at=time.perf_counter())
                if s.error: self._state['error']=f'native MIDI scheduler error ({s.error})'
                if hasattr(self._session,'diagnostics'):self._state['midi_diagnostics']=self._session.diagnostics()
            return SimpleNamespace(**self._state)
    def _run(self):
        try:
            from .native_metronome import metronome_beats
            session=self._factory(self._prepared,duration=self._intent.get('midi_duration',0))
            click_tempo,click_on,click_problem=None,False,None
            with self._lock: self._session=session; self._state['device']=session.device
            while not self._closing.is_set():
                try: delta,rev=self._queue.get(timeout=.02)
                except queue.Empty: continue
                commands=[]
                if delta.get('playing') is False:commands.append((2,0,0))
                if 'midi_gain' in delta: commands.append((7,0 if self._muted else delta['midi_gain'],0))
                if 'metronome' in delta:
                    tempo,on=delta['metronome']
                    if tempo!=click_tempo:
                        # Prime even while off, so the next flag-only change is O(1)
                        # native work. An invalid unused map need not stop playback.
                        beats=();click_problem=None
                        if tempo is not None:
                            try:beats=tuple(metronome_beats(tempo,session.duration))
                            except Exception as e:click_problem=f'invalid metronome tempo map: {e}'
                        if on and click_problem:raise ValueError(click_problem)
                        commands.append((None,beats,on))
                        click_tempo,click_on=tempo,on
                    elif on!=click_on:
                        if on and click_problem:raise ValueError(click_problem)
                        commands.append((_CLICK_GATE,int(on),0));click_on=on
                if 'rate' in delta: commands.append((5,delta['rate'],0))
                if 'loop' in delta:
                    a,b,on=delta['loop']; commands.append((6,a,b) if on else (8,0,0))
                if 'position' in delta: commands.append((3,delta['position'],0))
                if delta.get('playing') is True:commands.append((1,0,0))
                for cmd in commands:
                    if self._closing.is_set():break
                    seq=session.set_clicks(*cmd[1:]) if cmd[0] is None else session.command(*cmd)
                    while not self._closing.is_set():
                        receipt=session.receipt(seq)
                        if receipt==1: break
                        if receipt!=0: raise RuntimeError(f'native MIDI receipt {receipt}')
                        self._closing.wait(.002)
                    if self._closing.is_set(): break
                if not self._closing.is_set():
                    with self._lock:
                        self._intent.update(delta); self._state.update(intent_revision=rev,position_revision=rev,ready=True)
                        if 'position' in delta and self._pending_target and self._pending_target[1]==rev:
                            self._pending_target=None
        except Exception as e:
            with self._lock: self._state['error']=str(e)
        finally:
            with self._lock:
                session=self._session
                if session and session.handle:
                    try: self._state['position']=session.snapshot().seconds
                    except Exception: pass
                self._session=None
            try:
                released=session is None or session.close()
            except Exception as e:
                released=False
                with self._lock: self._state['error']='native MIDI close: '+str(e)
            with self._lock:
                self._failed_session=None if released else session
                if session and hasattr(session,'diagnostics'):
                    try:self._state['midi_diagnostics']=session.diagnostics()
                    except Exception:pass
                if session is None and self._state.get('error','') and 'release not confirmed' in self._state['error']: released=False
                # Closed target is a handoff contract, not a claim it was heard.
                if self._pending_target:
                    self._state['position'],self._state['position_revision']=self._pending_target
                self._state.update(closed=True,output_released=released,playing=False)
                if not released: self._state['error']='native MIDI output release not confirmed'
            if released: _LIVE.pop(id(self),None)

class SilentMidiSession(NativeMidiSession):
    """No device or WinMM library: real transport, discarded MIDI messages."""
    def __init__(self,*args,**kwargs):
        kwargs['silent']=True
        super().__init__(*args,**kwargs)
