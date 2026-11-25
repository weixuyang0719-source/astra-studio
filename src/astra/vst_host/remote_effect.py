"""Control a single insert worker. Its PCM path stays entirely in native code."""
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ctypes as C
import os
from threading import Lock, Timer
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .native import HostError, Plugin
from .session import PluginSession

_control = ThreadPoolExecutor(max_workers=8, thread_name_prefix='Insert control')


def bind(host):
    ptr=C.c_void_p
    defs = [('client',ptr,[ptr,C.c_wchar_p,C.c_uint32]),('fault',C.c_int,[ptr]),
            ('probe',C.c_int,[ptr]),('server',ptr,[ptr,C.c_wchar_p]),('stop',None,[ptr]),
            ('control',C.c_int,[ptr,C.c_int])]
    for title, result, args in defs:
        fn=getattr(host.dll,'astra_fx_'+title)
        fn.argtypes, fn.restype=args,result


def serve(plugin, name):
    if not isinstance(name,str) or not name.startswith('Local\\AstraInsert-') or len(name)!=50:
        raise ValueError('Invalid insert endpoint')
    bind(plugin.host)
    if getattr(plugin,'fx_server',None): raise HostError('Insert server already running')
    plugin.fx_server=plugin.host.dll.astra_fx_server(plugin.handle,name)
    if not plugin.fx_server: raise HostError('Cannot start insert audio server')
    return True


@contextmanager
def state_access(plugin):
    server=getattr(plugin,'fx_server',None)
    if server and not plugin.host.dll.astra_fx_control(server,1):
        raise HostError('Effect DSP did not release the state snapshot boundary')
    try:
        yield
    finally:
        if server:plugin.host.dll.astra_fx_control(server,0)


@dataclass
class PreparedPeer:
    peer: PluginSession
    name: str
    image: bytes
    generation: int
    latency: int
    factory_image: bytes | None
    timing: dict


def prepare_peer(library, path, class_id, rate, block, state, deadline_ns, standby=None, on_started=None):
    """Only the child owns vendor and GUI objects. Safe to start off the instrument thread."""
    from .insert_control import _check_prepare_deadline
    _check_prepare_deadline(deadline_ns)
    name='Local\\AstraInsert-'+uuid4().hex
    start=monotonic()
    peer=PluginSession(library,path,class_id,sample_rate=rate,max_block=block,effect=True,
                       standby=standby,on_started=on_started)
    try:
        _check_prepare_deadline(deadline_ns)
        saved=base64.b64encode(state).decode('ascii') if state is not None else None
        ready=peer.request('fx_prepare',name=name,state=saved)
        image=state if state is not None else base64.b64decode(ready['state'],validate=True)
        _check_prepare_deadline(deadline_ns)
        timing=dict(native_open_seconds=ready['native_open_seconds'], peer_ready_seconds=monotonic()-start,
                    used_standby=peer.used_standby, reused_instance=False)
        return PreparedPeer(peer,name,image,ready['state_generation'],ready['latency_samples'],
                            image if state is None else None,timing)
    except Exception as exc:
        from .insert_control import INSERT_EXPIRED
        code=peer.process.poll() if str(exc).startswith('Plugin process exited') else None
        peer.terminate()
        if str(exc)==INSERT_EXPIRED:raise
        raise peer.load_error(exc,'effect',code) from exc


class FavoriteEffect:
    """One explicit factory-state reservation per active instrument, never part of its audio graph."""
    def __init__(self, instrument, rate):
        self.library = instrument.host.dll._name
        self.rate, self.block = rate, instrument.max_block
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Preferred insert')
        self.future = self.identity = self.key = self.starting = None

    @staticmethod
    def identity_for(row):
        return os.path.normcase(str(Path(row['module']).resolve())), row['class_id'].upper()

    def select(self, key, row, standby):
        identity = self.identity_for(row) if row else None
        if (self.key, self.identity) == (key, identity):
            return
        self.clear()
        if row:
            self.key, self.identity = key, identity
            # Available immediately after process creation, even if a factory
            # initialization is stuck. Only this private child may be canceled.
            self.starting = child = dict(lock=Lock(), canceled=False, peer=None)
            def started(peer):
                with child['lock']:
                    if child['canceled']:
                        peer._canceled_start=True
                        raise HostError('Preferred effect selection changed')
                    child['peer']=peer
            self.future = self.pool.submit(prepare_peer, self.library, row['module'],
                row['class_id'], self.rate, self.block, None, None, standby, started)

    def status(self):
        future = self.future
        if future is None:
            return None
        result = dict(key=self.key, phase='loading')
        with self.starting['lock']:
            peer=self.starting['peer']
            if peer is not None:
                result['worker_pid']=peer.process.pid
        if future.done():
            error = future.exception()
            if error:
                result.update(phase='failed', error=str(error))
            else:
                peer = future.result().peer
                if peer.process.poll() is None:
                    result.update(phase='ready', worker_pid=peer.info['editor_worker_pid'])
                else:
                    result.update(phase='failed', error='效果器进程已退出')
        return result

    def take(self, key, row):
        if not self.future or self.key != key or self.identity != self.identity_for(row):
            return None
        future = self.future
        self.future = self.key = self.identity = self.starting = None
        if future.done() and future.exception() is not None:
            # The user clicked after a failed background attempt. Let this
            # explicit insertion retry immediately with a new, contained child.
            return None
        # A click during startup joins this same job. The instrument's scoped
        # transport thread continues accepting pause, play and seek commands.
        ready = future.result()
        if ready.peer.process.poll() is not None:
            ready.peer.terminate()
            return None
        ready.timing.update(preselected=True)
        return ready

    def clear(self):
        future = self.future
        child=self.starting
        self.future = self.key = self.identity = self.starting = None
        if future is not None and not future.cancel():
            if not future.done():
                with child['lock']:
                    child['canceled']=True
                    peer=child['peer']
                if peer is not None:
                    _control.submit(peer.cancel_startup)
            # Serialize disposal before any following opening. Never call a
            # vendor or block the instrument control thread while disabling.
            def dispose():
                try:
                    ready = future.result()
                except Exception:
                    return
                if ready.peer.process.poll() is None:
                    ready.peer.terminate()
            self.pool.submit(dispose)

    def close(self):
        self.clear()
        self.pool.shutdown(wait=False)


class ParkedEffect:
    """The previous user-selected effect, detached after a native graph boundary."""
    def __init__(self, effect, slot, key, restore_image=None):
        self.identity=effect.insert_identity
        self.slot=slot
        self.key=key
        self.restore_image=restore_image
        self.peer=effect.peer
        self.lock=Lock()
        self.claimed=False
        self.future=_control.submit(self._reset,effect.pending,effect.factory_image)
        self.timer=Timer(45,self.discard)
        self.timer.daemon=True
        self.timer.start()

    def _reset(self, outstanding, factory):
        try:
            if outstanding is not None:outstanding.result()
            name='Local\\AstraInsert-'+uuid4().hex
            restore=base64.b64encode(self.restore_image).decode('ascii') if self.restore_image is not None else None
            ready=self.peer.request('fx_rearm', name=name,restore_state=restore)
            timing=dict(reused_instance=True, used_standby=False)
            image=self.restore_image if self.restore_image is not None else factory
            return PreparedPeer(self.peer,name,image,ready['state_generation'],ready['latency_samples'],factory,timing)
        except BaseException:
            self.peer.terminate()
            raise

    def take(self):
        with self.lock:
            if self.claimed:return None
            self.claimed=True
            self.timer.cancel()
        try:return self.future.result()
        except (RuntimeError,ValueError,OSError):return None

    def discard(self):
        with self.lock:
            if self.claimed:return
            self.claimed=True
            self.timer.cancel()
        _control.submit(self.peer.terminate)


class RemoteEffect(Plugin):
    def __init__(self, instrument, path, class_id, rate, state, deadline_ns=None, *, prepared=None):
        from .insert_control import _check_prepare_deadline
        instrument._check()
        self.peer=None
        host=instrument.host
        bind(host)
        if prepared is None:
            prepared=prepare_peer(host.dll._name,path,class_id,rate,instrument.max_block,state,deadline_ns,
                                  getattr(instrument,'insert_standby',None))
        peer=prepared.peer
        handle=None
        try:
            _check_prepare_deadline(deadline_ns)
            handle=host.dll.astra_fx_client(instrument.handle,prepared.name,prepared.latency)
            if not handle or host.dll.astra_fx_probe(handle):raise HostError('Isolated effect warmup failed')
            _check_prepare_deadline(deadline_ns)
        except Exception as exc:
            if handle:host.dll.astra_vst_close(handle)
            peer.terminate()
            from .insert_control import INSERT_EXPIRED
            if str(exc)==INSERT_EXPIRED:raise
            raise peer.load_error(exc,'probe') from exc
        super().__init__(host,handle,instrument.max_block)
        self.peer=peer
        self.worker_pid=peer.info['editor_worker_pid']
        self.peer.timeout=5
        self.image=prepared.image
        self.factory_image=prepared.factory_image
        self.timing=prepared.timing
        self.insert_identity=(os.path.normcase(str(Path(path).resolve())),class_id.upper())
        self.failed=False
        self.generation=prepared.generation
        self.editor_open=False
        self.editor_seen=False
        self.editor_error=None
        self.editor_in_flight=False
        self.editor_space_scope=None
        self.pending=None
        self.pending_action=None
        self.last_poll=0.

    def _fail(self):
        if not self.failed:
            self.failed=True
            self.editor_open=False
            _control.submit(self.peer.terminate)

    def _status(self):
        s=self.peer.request('rt_status')
        generation=s['state_generation']
        image=None
        if self.generation!=generation or (self.editor_open and not s.get('editor_open')):
            reply=self.peer.request('state_capture')
            generation=reply['state_generation']
            image=base64.b64decode(reply['state'],validate=True)
        return s.get('editor_open') is True,generation,image

    def poll(self):
        if self.failed or not self.handle:return
        if self.host.dll.astra_fx_fault(self.handle) or self.peer.process.poll() is not None:
            self._fail()
            return
        if self.pending and not self.pending.done():return
        if self.pending:
            future, self.pending = self.pending,None
            try:
                value=future.result()
                opened,generation,image=value[:3]
                if self.editor_in_flight:self.editor_error=value[3]
            except (ValueError,RuntimeError,OSError):
                self._fail()
                return
            self.editor_in_flight=False
            self.editor_open=opened
            if generation>=self.generation:
                self.generation=generation
                if image is not None:self.image=image
        if self.pending_action is not None:
            target,self.pending_action=self.pending_action,None
            self.editor_in_flight=True
            self.pending=_control.submit(self._editor_request,*target)
        elif monotonic()-self.last_poll>=.25:
            self.last_poll=monotonic()
            self.pending=_control.submit(self._status)

    def _editor_request(self,opened,gesture,space_scope):
        error=None
        try:
            ack=(self.peer.request('editor_open',_relay_editor_gesture=gesture or {},
                                   editor_space_scope=space_scope) if opened
                 else self.peer.request('editor_close'))
            if ack.get('available') is False:error='插件没有提供可用的编辑窗口。'
            elif ack.get('error'):error=str(ack['error'])
        except (RuntimeError,ValueError,OSError) as exc:
            # A responsive effect can reject its editor and continue processing audio.
            # An independently failed status below still triggers normal containment.
            error=str(exc)
        return (*self._status(),error)

    @property
    def editor_pending(self):
        return not self.failed and (self.pending_action is not None or self.editor_in_flight)

    def remote_editor(self,opened,gesture=None):
        if self.failed:raise HostError('效果器已失去响应，请重新挂载。')
        if opened:self.editor_seen=True
        self.editor_space_scope=uuid4().hex if opened else None
        self.pending_action=(opened,gesture,self.editor_space_scope)
        self.poll()

    def state_generation(self):
        self.poll()
        return self.generation or 0

    def get_state(self):
        # This path is also called immediately before an explicit project save;
        # fetch the latest edit in that case. A dead effect retains its last
        # confirmed opaque state so the project can be reopened.
        if not self.failed:
            try:
                reply=self.peer.request('state_capture')
                image=base64.b64decode(reply['state'],validate=True)
                generation=reply['state_generation']
                # Some vendors serialize volatile IDs on every save. Save that
                # exact blob, but preserve the acknowledged handover image until
                # an editor has been used or the vendor reports a change.
                if self.editor_seen or generation!=self.generation:
                    self.image=image
                    self.generation=generation
                return image
            except (RuntimeError, ValueError, OSError):
                if self.peer.process.poll() is None:raise
                self._fail()
        return self.image

    def handover_image(self):
        # Editors can change opaque settings without an edit notification.
        return self.get_state() if self.editor_seen else self.image

    def park(self,slot,key,restore_image=None):
        self._check()
        if (self.failed or (self.factory_image is None and restore_image is None) or self.editor_in_flight or
                self.host.dll.astra_fx_fault(self.handle) or self.peer.process.poll() is not None):
            return None
        # The previous chain has been retired. Release its native proxy before
        # stopping the server so no callback can write to the old mapping.
        self.host.dll.astra_vst_close(self.handle)
        self.handle=None
        return ParkedEffect(self,slot,key,restore_image)

    def close(self):
        if self.handle:
            self._check()
            self.host.dll.astra_vst_close(self.handle)
            self.handle=None
            # The published rack may still reference the native slot until the
            # next callback. A missing server falls back to the same dry block.
            _control.submit(self.peer.close)
