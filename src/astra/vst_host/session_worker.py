"""Dedicated plugin/UI process. Only the native device callback renders RT audio."""
import base64
from collections import deque
from functools import partial
import json
import os
import queue
import sys
import threading
from time import monotonic, monotonic_ns
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from .native import NativeHost
from .editor_transport_wire import EditorSpace, valid_scope

_EOF = object()
_DRAIN_COUNT = 32
_DRAIN_SECONDS = 0.004
_EXCLUSIVE = ('state_get', 'state_capture', 'state_set', 'render_offline')
_SUBMISSIONS = {
    'rt_publish': ('publish', 'timeline'),
    'rt_transport': ('transport', 'command'),
    'rt_audition': ('audition', 'command'),
    'rt_mix': ('set_mix', 'mix'),
}


def _message(exc):
    return str(exc) or type(exc).__name__


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be a JSON object')
    return value


def _lifecycle_result(value):
    result = _object(value, 'Realtime lifecycle result')
    if result.get('accepted') is False:
        raise RuntimeError(result.get('error') or 'Realtime lifecycle request rejected')
    return result


def _encode(data):
    return base64.b64encode(data).decode('ascii')


class Worker(QObject):
    _wake = Signal()

    def __init__(self, host, plugin, window, sample_rate, send, quit_app):
        super().__init__()
        self._host, self._plugin, self._window = host, plugin, window
        self._sample_rate = sample_rate
        self._send, self._quit = send, quit_app
        self._inbox = queue.SimpleQueue()
        self._barrier_request = None
        plugin.host_rate=sample_rate
        self._scoped_inserts = (os.environ.get('ASTRA_VST_SCOPED_INSERT_TRANSPORT') == '1' or
            getattr(host,'isolated_inserts',False)) and host.native_insert_transactions
        self._wake_lock = threading.Lock()
        self._wake_queued = False
        self._wake.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        # UI-thread-only state. _busy also prevents a nested Qt event loop
        # inside plugin code from dispatching a resume or unload reentrantly.
        self._busy = self.finished = False
        self._deferred = deque()
        self._deferred_error = None
        self._park = None
        self._shutdown = []
        self.lane = None
        self._pcm = None
        self._retiring = self._close_sent = False
        self._fault = self._status_error = self._close_error = None
        self._view = {}
        self._receipts = {}
        self._receipt_seen = set()
        self._lane_id = None
        self._latency = host.dll.astra_vst_latency(plugin.handle)
        self._inserts = None
        self._insert_waiting = deque()
        self._insert_windows = {}
        self._open_slot_instances = {}
        self._editor_state = dict(revision=0, instrument=False, inserts={})
        self._window.observed.connect(lambda opened: self._editor_changed(None, self._plugin, None, opened))
        self._rack_generation = 0
        self._rack_observed = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(5)
        self._poll_timer.timeout.connect(self._poll)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._feedback_timer.setInterval(16)
        self._feedback_timer.timeout.connect(self._feedback_turn)
        self._window.observed.connect(self._sync_feedback_timer)

    def post(self, request):
        """Called by the pipe reader. It never touches a plugin or a lane."""
        self._inbox.put(request)
        self._notify()

    def _notify(self):
        with self._wake_lock:
            if self._wake_queued:
                return
            self._wake_queued = True
        self._wake.emit()

    def _sync_feedback_timer(self, *_):
        # Effect workers do not open a device, so the realtime-lane timer
        # never runs here. VST3 controller calls belong to this UI thread.
        if self.finished or not getattr(self,'_is_effect',False):
            return
        active = self._plugin.feedback_idle()
        if active and not self._feedback_timer.isActive():
            self._feedback_timer.start()
        elif not active and self._feedback_timer.isActive():
            self._feedback_timer.stop()

    @Slot()
    def _feedback_turn(self):
        if self._busy or self.finished:
            return
        self._busy = True
        try:
            self._plugin.feedback_idle(True)
        finally:
            self._leave_turn()

    def _leave_turn(self):
        self._busy = False
        self._sync_feedback_timer()
        if not self.finished and (not self._inbox.empty() or self._barrier_request is not None or (self._park is None and not self._pcm_parking() and self._deferred)):
            self._notify()

    @Slot()
    def _drain(self):
        with self._wake_lock:
            self._wake_queued = False
        if self._busy or self.finished:
            return
        self._busy = True
        try:
            deadline = monotonic() + _DRAIN_SECONDS
            for _ in range(_DRAIN_COUNT):
                if self._park is None and not self._pcm_parking() and self._deferred:
                    request = self._deferred.popleft()
                    if self._deferred_error is not None:
                        self._reply(request['request'], error=self._deferred_error)
                        if not self._deferred:
                            self._deferred_error = None
                    else:
                        self._dispatch(request)
                else:
                    try:
                        if self._barrier_request is not None:
                            request, self._barrier_request = self._barrier_request, None
                        else:
                            request = self._inbox.get_nowait()
                    except queue.Empty:
                        break
                    self._dispatch(request)
                self._finish_shutdown()
                if self.finished or monotonic() >= deadline:
                    break
        finally:
            self._leave_turn()

    def _reply(self, ident, result=None, *, error=None):
        if ident is None:  # stdin EOF is local, not an IPC request
            return
        if error is None:
            self._send({'request': ident, 'ok': True, 'result': result})
        else:
            self._send({'request': ident, 'ok': False, 'error': error})

    def _dispatch(self, request):
        if request is _EOF:
            ident, command, args = None, 'close', {}
        else:
            if not isinstance(request, dict):
                return
            ident = request.get('request')
            if type(ident) is not int or ident <= 0:
                return
            command, args = request.get('command'), request.get('arguments', {})
        try:
            if not isinstance(command, str):
                raise ValueError('Invalid plugin command')
            _object(args, 'arguments')
            if command in ('status', 'rt_status'):
                self._ack_receipts(args)
                self._refresh()
                result = self._rt_status()
                if command == 'status':
                    if self.lane is None and self._plugin.handle:
                        self._latency = self._host.dll.astra_vst_latency(self._plugin.handle)
                        result['plugin_latency_samples'] = self._latency
                    result = dict(loaded=bool(self._plugin.handle), editor_worker_pid=os.getpid(), editor_open=self._editor_state['instrument'],
                                  latency_samples=self._latency, realtime_ready=result['realtime_ready'], realtime=result)
                    if getattr(self,'_instrument_startup',None) is not None:
                        result['instrument_startup']=self._instrument_startup
                    if getattr(self, '_standby_idle_ms', None) is not None:
                        result['standby_idle_ms'] = self._standby_idle_ms
                if command == 'rt_status':
                    result['editor_open'] = self._editor_state['instrument']
                if args.get('known_editor_revision') != self._editor_state['revision']:
                    result['editor_state'] = self._editor_state
                result['worker_origin'] = __file__
                result['worker_protocol'] = 'astra-vst-session-rt-1'
            elif command == 'close':
                if ident is None:
                    self._shutdown.append(None)
                self._begin_retirement()
                if ident is not None:
                    self._shutdown.append(ident)
                return  # shutdown replies only after definitive release and plugin close
            elif command == 'rt_close':
                self._begin_retirement()
                result = self._rt_status()
            elif self._shutdown or self._retiring:
                raise RuntimeError('Plugin session or realtime device is closing; request not run')
            elif command in ('insert_publish','insert_action') and self._insert_pending():
                self._insert_waiting.append(request)
                return
            elif command == 'rt_pcm':
                if self.lane is None or self._fault:
                    raise RuntimeError(self._fault or 'Realtime lane is not open')
                if self._pcm is None:
                    from .pcm_source import PcmSourceControl
                    self._pcm = PcmSourceControl(self.lane, int(self._sample_rate))
                result = self._pcm.set(args.get('source'), args.get('epoch'))
            elif self._park is not None or self._pcm_parking():
                # Retain mutation order. Only status and retirement may bypass
                # work that is waiting for an exact processing-exclusion grant.
                self._deferred.append(request)
                return
            elif command in _EXCLUSIVE:
                self._exclusive(ident, command, args)
                return
            else:
                result = self._ordinary(command, args)
        except Exception as exc:
            self._reply(ident, error=_message(exc))
        else:
            self._reply(ident, result)

    def _pcm_parking(self):
        return self._pcm is not None and self._pcm.ticket is not None

    def _insert_pending(self):
        return self._inserts is not None and self._inserts.instrument.inserts_status()['pending_revision'] != 0

    def _rack_generation_now(self):
        value = (self._plugin.state_generation(), self._inserts.status()['generation'] if self._inserts else 0)
        if value != self._rack_observed:
            self._rack_observed = value
            self._rack_generation += 1
        return self._rack_generation

    def _advance_inserts(self):
        if self._inserts:
            for row in self._inserts.slots:
                sid=row['slot_id']
                plugin=self._inserts.plugins.get(sid)
                if hasattr(plugin,'remote_editor'):
                    plugin.poll()
                    self._editor_changed(sid, plugin, row['plugin']['key'], plugin.editor_open)
                    scope = getattr(plugin, 'editor_space_scope', None)
                    if scope and (plugin.editor_open or plugin.editor_pending):
                        for event in plugin.peer.take_editor_space():
                            if (self._editor_transport_ready() and event.scope == scope
                                    and event.pid == plugin.worker_pid and event.current()):
                                self._send(event.encode(scope=self._lane_id))
        if self._insert_waiting and not self._insert_pending() and not self._retiring:
            self._dispatch(self._insert_waiting.popleft())

    def _editor_transport_ready(self):
        return (not self.finished and not self._shutdown and self._phase() == 'ready'
                and self._view.get('realtime_ready') is True and self._lane_id is not None)

    def _insert_space(self, sid, plugin, shell):
        if (not self._editor_transport_ready() or self._inserts is None
                or self._inserts.plugins.get(sid) is not plugin):
            return False
        self._send(EditorSpace(shell, os.getpid(), monotonic_ns(), self._lane_id).encode())
        return True

    def _remote_effect_space(self, scope, shell):
        if self.finished or self._shutdown or not getattr(self._plugin, 'fx_server', None):
            return False
        self._send(EditorSpace(shell, os.getpid(), monotonic_ns(), scope).encode())
        return True

    def _editor_changed(self, sid, plugin, key, opened):
        old = self._editor_state
        inst, slots = old['instrument'], old['inserts']
        if sid is None:
            if inst == opened: return
            inst = opened
        else:
            if opened:
                if not self._inserts or self._inserts.plugins.get(sid) is not plugin: return
                if self._open_slot_instances.get(sid) is plugin and slots.get(sid) == key: return
                self._open_slot_instances[sid] = plugin
                slots = dict(slots, **{sid: key})
            else:
                if self._open_slot_instances.get(sid) is not plugin: return
                del self._open_slot_instances[sid]
                slots = dict(slots)
                slots.pop(sid, None)
        # Immutable once published: a queued IPC reply must not change retrospectively.
        self._editor_state = dict(revision=old['revision']+1, instrument=inst, inserts=slots)

    def _editor_ack(self, value):
        return dict(value, editor_state=self._editor_state, editor_lane_id=self._lane_id)

    def _close_retired_insert_windows(self):
        for sid,(plugin,window) in list(self._insert_windows.items()):
            if self._inserts.plugins.get(sid) is not plugin:
                window.close()
                del self._insert_windows[sid]

    def _ordinary(self, command, args):
        if command == 'prepare_favorite_effect':
            if self._is_effect or not getattr(self._host,'isolated_inserts',False):
                return False
            key=args.get('key')
            row=args.get('row')
            if key is not None and (not isinstance(key,str) or not isinstance(row,dict)
                    or not row.get('module') or not row.get('class_id')):
                raise ValueError('Unknown preferred effect')
            if self._inserts is None:
                from .insert_control import InsertControl
                self._inserts=InsertControl(self._plugin,self._sample_rate)
            self._inserts.prepare_favorite(key,row if key is not None else None)
            return True
        if command == 'warm_insert_worker':
            if self._is_effect or not getattr(self._host,'isolated_inserts',False):
                return False
            from .session import PluginStandby
            if getattr(self._plugin,'insert_standby',None) is None:
                self._plugin.insert_standby=PluginStandby()
            self._plugin.insert_standby.prime()
            return True
        if command == 'park_navigation':
            if self.lane is not None or self._status_error or self._fault or self._shutdown:
                raise RuntimeError('Cannot park until the output lane has retired cleanly')
            if self._window and self._plugin.editor_open:
                if self._window.dismiss().get('editor_open'):
                    raise RuntimeError('Plugin editor is still open')
            if self._insert_pending():
                raise RuntimeError('Insert rack is not quiescent')
            return dict(sample_rate=self._sample_rate, max_block=self._plugin.max_block,
                        generation=self._rack_generation_now())
        if command == 'standby_instrument':
            if self.lane is not None or self._status_error or self._fault or self._shutdown:
                raise RuntimeError('Cannot retain an instrument until its audio lane has retired cleanly')
            if self._window and self._plugin.editor_open:
                if self._window.dismiss().get('editor_open'):
                    raise RuntimeError('Plugin editor is still open')
            if self._inserts is not None:
                self._inserts.standby_for_restore()
                self._close_retired_insert_windows()
            self._close_insert_standby()
            return True
        if command == 'snapshot_live_inserts':
            if not getattr(self._host, 'isolated_inserts', False):
                raise RuntimeError('Live insert snapshots require isolated effects')
            from .rack_state import encode
            def snapshot():
                slots, generation = self._inserts.capture(confirmed_images=True) if self._inserts else ([], 0)
                rack = dict(version=1, instrument=None, slots=slots)
                return dict(rack=_encode(encode(rack)), generation=generation)
            return self._with_scoped_transport(snapshot) if self.lane is not None else snapshot()
        if command == 'fx_serve':
            if self.lane is not None:raise RuntimeError('Effect PCM worker requires a closed endpoint')
            from .remote_effect import serve
            return serve(self._plugin,args['name'])
        if command in ('fx_prepare','fx_rearm'):
            if not self._is_effect:
                raise RuntimeError('Insert preparation requires an effect worker')
            if command=='fx_rearm':
                restore=args.get('restore_state')
                saved=self._plugin_factory if restore is None else base64.b64decode(restore,validate=True)
                if not self._plugin.fx_server or not saved:
                    raise RuntimeError('Cannot reset a non-factory or inactive effect')
                if self._plugin.editor_open and self._window.dismiss().get('editor_open'):
                    raise RuntimeError('Effect editor is still open')
                self._host.dll.astra_fx_stop(self._plugin.fx_server)
                self._plugin.fx_server=None
                self._plugin.set_state(saved,offline=True)
                self._plugin.reset_effect_history()
            if self.lane is not None or getattr(self._plugin, 'fx_server', None):
                raise RuntimeError('Insert preparation requires an unused effect worker')
            from .remote_effect import serve
            state = args.get('state')
            if state is not None:
                self._plugin.set_state(base64.b64decode(state, validate=True), offline=True)
            generation = self._rack_generation_now()
            if command=='fx_prepare' and state is None:
                self._plugin_factory=self._plugin.get_state()
            image = _encode(self._plugin_factory) if command=='fx_prepare' and state is None else None
            latency = self._host.dll.astra_vst_latency(self._plugin.handle)
            serve(self._plugin,args['name'])
            return dict(state=image, state_generation=generation, latency_samples=latency,
                        native_open_seconds=self._native_open_seconds)
        if command == 'candidate_warm_rack':
            if self.lane is not None:
                raise RuntimeError('Rack warmup requires a closed endpoint')
            return self._plugin.warm_candidate_chain()
        if command == 'insert_action':
            if args.get('candidate_only') is True and self.lane is not None:
                raise RuntimeError('Isolated insert preparation requires an unopened endpoint')
            from copy import deepcopy
            from .insert_control import InsertControl
            if self._inserts is None:self._inserts=InsertControl(self._plugin,self._sample_rate)
            from .rack_state import initial_slots
            slots=deepcopy(self._inserts.slots) or initial_slots()
            sid=args['slot_id']
            if not isinstance(sid,str) or not sid or len(sid)>80:raise ValueError('Invalid insert slot')
            index=next((i for i,s in enumerate(slots) if s['slot_id']==sid),None)
            if index is None:
                if len(slots)>=32:raise ValueError('Insert slot capacity reached')
                index=len(slots);slots.append(dict(slot_id=sid,bypassed=False,plugin=None))
            action=args['action'];catalog=_object(args['catalog'],'effect catalog')
            if action=='insert_add':
                deadline_ns = args.get('prepare_deadline_ns')
                if deadline_ns is not None and (type(deadline_ns) is not int or deadline_ns <= 0):
                    raise ValueError('Invalid effect preparation deadline')
                def add():
                    return self._inserts.add_live(slots,index,args['key'],catalog,deadline_ns=deadline_ns)
                if self._scoped_inserts and self.lane is not None:
                    if self._phase()!='ready' or self._barrier_request is not None or self._deferred:
                        raise RuntimeError('Realtime transport unavailable for the requested insert; keeping previous inserts')
                    result=self._with_scoped_transport(add)
                else:
                    result=add()
                self._close_retired_insert_windows()
                return dict(result,state_generation=self._rack_generation_now(),inserts=self._inserts.status())
            elif action=='insert_remove':slots[index]['plugin']=None
            elif action=='insert_bypass':
                if type(args.get('enabled')) is not bool:raise ValueError('Invalid bypass')
                slots[index]['bypassed']=args['enabled']
            elif action=='insert_move':
                target=args['to_index']
                if type(target) is not int or not 0<=target<len(slots):raise ValueError('Invalid insert order')
                slots.insert(target,slots.pop(index))
            else:raise ValueError('Unknown insert action')
            result=self._inserts.publish(slots,catalog,remember_slot=sid if action=='insert_remove' else None)
            self._close_retired_insert_windows()
            return dict(result,state_generation=self._rack_generation_now(),inserts=self._inserts.status())
        if command == 'insert_publish':
            from .insert_control import InsertControl
            from .rack_state import decode
            rack = decode(base64.b64decode(args['rack'], validate=True))
            catalog = _object(args['catalog'], 'effect catalog')
            if self._inserts is None:
                self._inserts = InsertControl(self._plugin, self._sample_rate)
            result=self._inserts.restore(rack['slots'], catalog)
            self._close_retired_insert_windows()
            return result
        if command == 'insert_editor':
            sid = args['slot_id']
            opening = args.get('open', True)
            if type(opening) is not bool: raise ValueError('Invalid editor action')
            plugin = self._inserts.plugins.get(sid) if self._inserts else None
            slot = next((s for s in self._inserts.slots if s['slot_id']==sid), {}) if self._inserts else {}
            key = (slot.get('plugin') or {}).get('key')
            if ('key' in args and key != args['key']) or ('rack_revision' in args and
                    args['rack_revision'] != getattr(self._inserts, 'revision', None)):
                return self._editor_ack(dict(skipped='slot_changed'))
            if plugin is None:
                raise ValueError('Effect slot is empty or unavailable')
            if hasattr(plugin,'remote_editor'):
                plugin.remote_editor(opening,args.get('editor_gesture'))
                return self._editor_ack(dict(pending=True))
            old = self._insert_windows.get(sid)
            if old is not None and old[0] is not plugin:
                old[1].close()
                old=None
            window=old[1] if old else None
            if not opening:
                return self._editor_ack(window.dismiss() if window else dict(available=True,editor_open=False))
            if window is None:
                from .editor_window import PluginEditorWindow
                window = PluginEditorWindow(plugin, 'VST Effect')
                window.observed.connect(partial(self._editor_changed, sid, plugin, key))
                self._insert_windows[sid] = (plugin,window)
            on_space = partial(self._insert_space, sid, plugin)
            return self._editor_ack(window.present(args.get('editor_gesture'), on_space=on_space))
        if command == 'rt_open':
            return self._open(args)
        if command in _SUBMISSIONS:
            if self.lane is None or self._fault:
                raise RuntimeError(self._fault or 'Realtime lane is not open')
            method, field = _SUBMISSIONS[command]
            value = _object(args[field], field)
            self._refresh()  # return receipt capacity even during a burst of submissions
            # Let the facade enforce sequence readiness and epoch/intent rules.
            # An acceptance is not an application receipt; never retry it here.
            return getattr(self.lane, method)(value)
        if command == 'editor_open':
            scope = args.get('editor_space_scope')
            gesture = args.get('editor_gesture')
            if getattr(self, '_is_effect', False) and valid_scope(scope):
                return self._editor_ack(self._window.present(gesture, on_space=partial(self._remote_effect_space, scope)))
            return self._editor_ack(self._window.present(gesture))
        if command == 'editor_close':
            return self._editor_ack(self._window.dismiss())
        raise ValueError('Unsupported plugin operation')

    def _with_scoped_transport(self, work):
        # The Qt worker owns vendor lifecycle end to end. Borrow only the native
        # transport producer while it cannot drain the wire. No Qt or vendor method
        # is called by that borrower. Other wire operations are ordered barriers.
        self._refresh()
        if self._phase() != 'ready':
            raise RuntimeError('Plugin transport is not available for insertion')
        snapshot = self._rt_status()
        lane, epoch = self.lane, self.lane._scoped_prepare()
        done, claimed = threading.Event(), threading.Event()
        faults = []

        def run():
            try:
                lane._scoped_claim(epoch)
            except Exception as exc:
                faults.append(exc)
                claimed.set()
                return
            claimed.set()
            try:
                while not done.is_set():
                    try:
                        request = self._inbox.get(timeout=.005)
                    except queue.Empty:
                        continue
                    if request is _EOF or not isinstance(request,dict):
                        self._barrier_request=request
                        return
                    if request.get('command') in ('warm_insert_worker','prepare_favorite_effect'):
                        self._reply(request.get('request'),False)
                        continue
                    if request.get('command') not in ('rt_transport','rt_status'):
                        self._barrier_request = request
                        return
                    ident = request.get('request')
                    if type(ident) is not int or ident <= 0:
                        continue
                    try:
                        args = _object(request.get('arguments',{}),'arguments')
                        if request['command']=='rt_status':
                            self._ack_receipts(args)
                            self._apply_refresh(lane._scoped_status(snapshot['state_generation']))
                            value = self._rt_status(snapshot)
                            value['editor_open'] = self._editor_state['instrument']
                            if args.get('known_editor_revision') != self._editor_state['revision']:
                                value['editor_state'] = self._editor_state
                            value.update(worker_protocol='astra-vst-session-rt-1',worker_origin=__file__)
                        else:
                            self._apply_refresh(lane._scoped_status(snapshot['state_generation']))
                            value = lane.transport(_object(args['command'],'transport'))
                    except Exception as exc:
                        self._reply(ident,error=_message(exc))
                    else:
                        self._reply(ident,value)
            finally:
                try:
                    lane._scoped_release(epoch)
                except Exception as exc:
                    faults.append(exc)

        thread = threading.Thread(target=run, name='astra-insert-control')
        try:
            thread.start()
        except BaseException:
            lane._scoped_reclaim(epoch,unclaimed=True)
            raise
        claimed.wait()
        if faults:
            thread.join()
            lane._scoped_reclaim(epoch,unclaimed=True)
            raise faults[0]
        try:
            return work()
        finally:
            done.set()
            thread.join()
            if faults:
                raise faults[0]
            lane._scoped_reclaim(epoch)

    def _open(self, args):
        if self.lane is not None:
            raise RuntimeError('Retire the existing realtime lane before opening another')
        route = _object(args['route'], 'route')
        if route['sample_rate'] != self._sample_rate:
            raise ValueError('Route sample rate differs from the plugin setup; reopen the plugin session at the selected rate')
        frames = route['buffer_frames']
        if type(frames) is int and frames > self._plugin.max_block and not route.get('kept_endpoint'):
            raise ValueError('Route buffer exceeds the plugin max_block; reopen the plugin session with a compatible setup')
        libraries = {key: args[key] for key in ('bridge_library', 'device_library', 'portaudio_library')}
        if not all(isinstance(path, str) and path for path in libraries.values()):
            raise ValueError('Realtime library paths must be configured path strings')
        self._fault = self._status_error = self._close_error = None
        try:
            # The legacy session also works in a build without the RT component.
            # An explicit RT request reports an import/open error, never a fallback.
            from .realtime import RealtimeLane
            self.lane = RealtimeLane(self._plugin, **libraries)
        except Exception as exc:
            self._fault = _message(exc)
            raise
        self._receipt_seen.clear()
        self._lane_id = uuid4().hex
        self._receipts.clear()  # native command IDs are scoped to this lane lifetime
        self._view = {}
        self._poll_timer.start()
        try:
            result = _lifecycle_result(self.lane.open_device(route))
        except Exception as exc:
            self._fault = _message(exc)
            # A failed open can still hold a native lease. Initiate retirement
            # and keep both objects alive even if initiation itself fails.
            try:
                self._begin_retirement()
            except Exception:
                pass  # recorded by _begin_retirement; explicit close can retry
            raise
        self._refresh()
        return result

    def _ack_receipts(self, args):
        ids = args.get('ack_receipts', [])
        if not isinstance(ids, list) or any(type(x) is not int or x <= 0 for x in ids):
            raise ValueError('ack_receipts must contain positive command IDs')
        if args.get('lane_id') != self._lane_id or self._lane_id is None:
            return
        for ident in ids:
            self._receipts.pop(ident, None)

    def _refresh(self):
        if self.lane is None:
            return
        try:
            status = dict(_object(self.lane.status(), 'Realtime status'))
            self._apply_refresh(status)
        except Exception as exc:
            self._status_error = f'Realtime status unavailable: {_message(exc)}'

    def _apply_refresh(self,status):
        current_receipts = status.pop('receipts', ())
        seen = set()
        for receipt in current_receipts:
            ident = receipt['command_id']
            if ident not in self._receipt_seen:
                self._receipts[ident] = dict(receipt)
            seen.add(ident)
        self._receipt_seen = seen
        latency = status.get('plugin_latency_samples')
        if type(latency) is int and latency >= 0:
            self._latency = latency
        self._view = status
        self._status_error = None

    def _phase(self):
        if self._retiring:
            return 'closing'
        if self._fault or (self.lane is not None and self._status_error):
            return 'error'
        if self.lane is None:
            return 'closed'
        phase = self._view.get('phase', 'opening')
        # The device snapshot alone is not a lifetime acknowledgement.
        return phase if phase in ('opening', 'ready', 'closing', 'error') else 'error'

    def _rt_status(self, scoped=None):
        """position_samples describes the rendered/project clock, not the DAC."""
        view, phase = self._view, self._phase()
        transport = dict(state='stopped', position_samples=0, position_fraction=0, rate=1., last_intent=None, loop_enabled=False)
        observed = view.get('transport') or {}
        for key in transport:
            if key in observed:
                transport[key] = observed[key]
        device = view.get('device')
        if device is not None:
            device = {key: device[key] for key in ('backend', 'device_id', 'output_channels', 'sample_rate', 'buffer_frames') if key in device}
            if 'output_channels' in device:
                device['output_channels'] = list(device['output_channels'])
        last_error = self._close_error or self._fault or self._status_error or view.get('last_error')
        if phase == 'error' and last_error is None:
            last_error = 'Realtime lane is not usable; retire it before reopening'
        # The app gates the score metronome and count-in on these lane flags.
        # Forward only those two; the wide score-rate path keeps its old default.
        lane_caps = view.get('capabilities') or {}
        capabilities = dict(score_click_gates=bool(lane_caps.get('score_click_gates')),
                            score_count_in=bool(lane_caps.get('score_count_in')))
        return dict(version=1, capabilities=capabilities, lane_id=self._lane_id, phase=phase, realtime_ready=(phase == 'ready' and self._park is None and view.get('realtime_ready') is True),
                    insert_scoped_transport=self._scoped_inserts, isolated_inserts=getattr(self._host,'isolated_inserts',False),
                    epoch=view.get('epoch'), submitted_sequence=view.get('submitted_sequence'), applied_sequence=view.get('applied_sequence'),
                    transport=transport, device=device, plugin_latency_samples=self._latency,
                    output_latency_samples=view.get('output_latency_samples'), underruns=view.get('underruns', 0),
                    last_error=last_error, pcm=scoped['pcm'] if scoped is not None else self._pcm.status() if self._pcm else dict(state='off',epoch=None,error=None), state_generation=scoped['state_generation'] if scoped is not None else self._rack_generation_now(), inserts=scoped['inserts'] if scoped is not None else self._inserts.status() if self._inserts else None, mix=view.get('mix'), receipts=list(self._receipts.values()))

    def _exclusive(self, ident, command, args):
        plugin = self._plugin
        binary_result = command not in ('state_set', 'state_capture')
        if command == 'state_capture':
            def work():
                before = self._rack_generation_now()
                result = dict(state_generation=before)
                token = args.get('initial_token')
                if (token and self.lane is None and
                        getattr(self, '_initial_image', None) == (token, plugin.state_generation())):
                    result['initial_token'] = token
                else:
                    result['state'] = _encode(plugin.get_state())
                # Notifications during get_state remain newer than this watermark.
                # Never label an earlier component/controller image with a later generation.
                plugin.state_generation()
                if self._inserts is not None:
                    from .rack_state import encode
                    confirmed=(args.get('newly_prepared_inserts') is True and self.lane is None
                               and getattr(self._host,'isolated_inserts',False))
                    slots, _ = self._inserts.capture(confirmed_images=confirmed)
                    result['inserts'] = _encode(encode(dict(version=1,instrument=None,slots=slots)))
                return result
        elif command == 'state_get':
            work = plugin.get_state
        elif command == 'state_set':
            # Decode before acquiring processing exclusion, not while parked.
            blob = base64.b64decode(args['state'], validate=True)
            token = args.get('initial_token')
            if token is not None and (not isinstance(token,str) or len(token)!=32 or self.lane is not None):
                raise ValueError('Invalid initial state token')
            def work():
                self._initial_image = None
                if self.lane is None and not getattr(plugin, 'fx_server', None):
                    # A restored project can load vendor resources after the
                    # factory handshake. No graph is rendering in this worker.
                    from .session_entry import PreparationQoS
                    with PreparationQoS():
                        plugin.set_state(blob, offline=True)
                else:
                    plugin.set_state(blob, offline=self.lane is None)
                if token:
                    self._initial_image = (token, plugin.state_generation())
        else:
            work = partial(plugin.render_offline, args['frames'], args.get('events', []),
                           args.get('sample_position', 0), args.get('tempo', 120), args.get('playing', True),
                           unattached=self.lane is None)
        if command == 'state_capture':
            # VST3 getState is a UI-thread read explicitly permitted during process().
            # Drain serializes all UI operations; the native audio callback needs
            # neither our opaque state buffer nor the controller. Never park the
            # instrument or bypass a remote insert just to serialize a project.
            # State writes, offline rendering and teardown still require exclusion.
            if self.lane is not None:
                self._refresh()
                if self._phase() != 'ready':
                    raise RuntimeError('Wait for realtime readiness or retire the device before plugin state capture')
            self._reply(ident, work())
            return
        if self.lane is None:
            if getattr(plugin,'fx_server',None):
                if command=='render_offline':
                    raise RuntimeError('The insert server owns effect rendering')
                from .remote_effect import state_access
                with state_access(plugin):
                    data=work()
            else:
                data = work()
            self._reply(ident, _encode(data) if binary_result else data)
            return
        self._refresh()
        if self._phase() != 'ready':
            raise RuntimeError('Wait for realtime readiness or retire the device before plugin state/offline control')
        try:
            ticket = _lifecycle_result(self.lane.begin_control())['ticket']
            if type(ticket) is not int:
                raise ValueError('Invalid realtime control ticket')
        except Exception as exc:
            self._fault = f'Realtime park failed; retire the lane: {_message(exc)}'
            raise RuntimeError(self._fault) from exc
        self._park = (ident, ticket, work, binary_result)

    def _cancel_waiting(self, error):
        # No plugin operation is in progress here. Retirement does not depend
        # on a park grant and must also work if callbacks can no longer ack it.
        if self._park is not None:
            ident = self._park[0]
            self._park = None
            self._reply(ident, error=error)
        while self._insert_waiting:
            request=self._insert_waiting.popleft()
            self._reply(request['request'],error=error)
        if self._deferred:
            # Return canceled requests in bounded batches, not inline here.
            self._deferred_error = error

    def _advance_control(self):
        ident, ticket, work, binary_result = self._park
        try:
            if self.lane.control_ready(ticket) is not True:
                if self._view.get('phase') in ('closing', 'closed', 'error'):
                    raise RuntimeError(self._view.get('last_error') or 'Device became unavailable before the park grant')
                return
        except Exception as exc:
            self._fault = f'Cannot confirm realtime park; retire the lane: {_message(exc)}'
            self._cancel_waiting(self._fault)
            return
        try:
            try:
                data = work()
            finally:
                try:
                    _lifecycle_result(self.lane.end_control(ticket))
                except Exception as exc:
                    self._fault = f'Plugin call may have run, but realtime resume failed; retire the lane: {_message(exc)}'
                    raise RuntimeError(self._fault) from exc
            self._reply(ident, _encode(data) if binary_result else data)
        except Exception as exc:
            self._reply(ident, error=_message(exc))
        finally:
            self._park = None
        if self._fault:
            self._cancel_waiting(self._fault)

    def _begin_retirement(self):
        if self.lane is None:
            self._fault = self._status_error = self._close_error = None
            return
        if not self._retiring:
            self._retiring = True
            self._cancel_waiting('Realtime retirement requested; pending plugin operation not run')
        if self._close_sent:
            return
        self._refresh()
        try:
            _lifecycle_result(self.lane.close_device())
        except Exception as exc:
            self._close_error = f'Could not initiate realtime retirement: {_message(exc)}'
            raise RuntimeError(self._close_error) from exc
        self._close_sent = True
        self._close_error = None

    def _advance_retirement(self):
        try:
            if self.lane.close_ready() is not True:
                return
        except Exception as exc:
            self._close_error = f'Cannot confirm realtime retirement: {_message(exc)}'
            return
        # A may cache the final receipts in the retired facade. Collect them
        # before discarding it. Neither a status phase nor an exception grants release.
        self._refresh()
        self._status_error = self._status_error or self._fault or self._view.get('last_error')
        self._fault = self._close_error = None
        if self._pcm is not None:
            self._pcm.retire()
            self._pcm = None
        self.lane = None
        self._retiring = self._close_sent = False
        self._view = {}
        self._poll_timer.stop()

    @Slot()
    def _poll(self):
        if self._busy or self.finished or self.lane is None:
            return
        self._busy = True
        try:
            self._refresh()  # also drain receipts with no client polling status
            if self._retiring:
                self._advance_retirement()
            elif self._park is not None:
                self._advance_control()
            elif self._pcm is not None:
                self._pcm.poll(int((self._view.get('transport') or {}).get('position_samples', 0)))
            self._advance_inserts()
            self._finish_shutdown()
        finally:
            self._leave_turn()

    def _finish_shutdown(self):
        if not self._shutdown or self.lane is not None or self._deferred or self.finished:
            return
        self.finished = True
        self._feedback_timer.stop()
        error = None
        try:
            self._close_insert_standby()
            self._window.close()
            for _,window in self._insert_windows.values():
                window.close()
            self._plugin.close()
            if self._inserts is not None:
                self._inserts.close()
        except Exception as exc:
            error = _message(exc)
        for ident in self._shutdown:
            self._reply(ident, error=error)
        self._quit()

    def _close_insert_standby(self):
        standby=getattr(self._plugin,'insert_standby',None)
        if standby:
            standby.close()
            self._plugin.insert_standby=None


def main(preparation=None, requests=None, replies=None):
    if requests is None or replies is None:
        from .session_entry import private_wire
        requests,replies=private_wire()
    job_name = os.environ.pop('ASTRA_VST_PRIVATE_JOB', None)
    if sys.platform == 'win32' and job_name:
        from astra.desktop_worker import join_managed_job
        join_managed_job(job_name)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    standby_idle_ms = None
    if sys.argv[1:] == ['--standby']:
        # No endpoint, module, class or preset has been chosen. Keep stdout
        # exclusively for the existing request-ID protocol after assignment.
        if preparation:
            preparation.restore()
        idle_start = monotonic()
        line = requests.readline(65536)
        standby_idle_ms = round((monotonic() - idle_start) * 1000)
        if not line:
            return 0
        selected = json.loads(line)
        if (not isinstance(selected, list) or len(selected) not in (7, 9)
                or not all(isinstance(value, str) for value in selected)
                or selected[5] not in ('instrument', 'effect')
                or (selected[5] == 'effect' and len(selected) != 7)):
            return 1
        sys.argv[1:] = selected
        if preparation:
            preparation.activate()
    if sys.platform == 'win32' and len(sys.argv) > 7:
        from . import editor_win32
        from .editor_peer import studio_for_worker
        editor_win32.STUDIO_PEER = studio_for_worker(int(sys.argv[7]))
    print('{"startup_stage":"host"}',file=replies,flush=True)
    try:
        host = NativeHost(sys.argv[1])
    except (OSError,AttributeError) as exc:
        print(json.dumps(dict(startup_error=True,code='host_binary',error='Host library unavailable',
                              winerror=getattr(exc,'winerror',None))),file=replies,flush=True)
        return 1
    rate = float(sys.argv[4])
    opener=host.open_effect if len(sys.argv)>6 and sys.argv[6]=='effect' else host.open
    receipt=None
    native_start=monotonic()
    try:
        print('{"startup_stage":"plugin"}',file=replies,flush=True)
        if len(sys.argv)==10 and sys.argv[6]=='instrument':
            from .instrument_start import open_instrument
            plugin,receipt = open_instrument(host,sys.argv[2],sys.argv[3],rate,int(sys.argv[5]),int(sys.argv[8]),int(sys.argv[9]))
        else:
            plugin = opener(sys.argv[2], sys.argv[3], rate, int(sys.argv[5]))
    except Exception as exc:
        from .native import HostError
        code = 'setup_processing_rejected' if isinstance(exc, HostError) and str(exc) == 'Plugin setupProcessing failed' else 'initialization_failed'
        print(json.dumps(dict(startup_error=True, code=code, error=str(exc),instrument_startup=getattr(exc,'instrument_startup',None)), allow_nan=False),file=replies,flush=True)
        return 1

    native_open_seconds=monotonic()-native_start
    print('{"startup_stage":"handshake"}',file=replies,flush=True)
    from .editor_window import PluginEditorWindow
    window = PluginEditorWindow(plugin, 'VST Effect' if len(sys.argv)>6 and sys.argv[6]=='effect' else 'VST Instrument')
    responses = queue.SimpleQueue()
    worker = Worker(host, plugin, window, rate, responses.put, app.quit)
    worker._instrument_startup=receipt
    worker._is_effect=sys.argv[6]=='effect'
    worker._sync_feedback_timer()
    worker._plugin_factory=None
    worker._native_open_seconds=native_open_seconds
    worker._standby_idle_ms=standby_idle_ms
    if preparation:
        preparation.restore()

    def read():
        try:
            for line in requests:
                try:
                    request = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                worker.post(request)
        finally:
            worker.post(_EOF)

    def write():
        while True:
            response = responses.get()
            if response is None:
                return
            try:
                line = json.dumps(response, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                if 'request' not in response:
                    continue
                line = json.dumps(dict(request=response['request'], ok=False, error='Plugin result is not a JSON value'))
            try:
                print(line,file=replies,flush=True)
            except (OSError, ValueError):
                return

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    threading.Thread(target=read, daemon=True).start()
    try:
        return app.exec()
    finally:
        if worker.lane is not None:
            # Unexpected Qt exit with an unretired lane. Contain the process;
            # do not run local plugin/DLL teardown against a possible callback.
            os._exit(1)
        try:
            if not worker.finished:
                plugin.close()
        finally:
            responses.put(None)
            writer.join()  # flush final replies after leaving the Qt loop


if __name__ == '__main__':
    raise SystemExit(main())
