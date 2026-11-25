"""Product control lane, not a renderer and not a second plugin worker.

GUI owns admission, current intent, and observations. The supplied desktop
executor performs each synchronous PluginSession call. At most one session
call and one Frozen conversion are in flight; only the latter is latest-only.
"""
from collections import deque
from concurrent.futures import CancelledError, Future
from time import perf_counter
from uuid import uuid4

from astra.vst_host.session import PluginSession
from astra.vst_host.realtime import VST_MIDI_GAIN_MAX, VST_RATE_UNIT
from .vst_project import capture_instrument, capture_state, restore_instrument, restore_initial_state
from .vst_projection import Projector, sample_of


_GAIN_FIELDS = ('master_gain','midi_gain','audio_gain','click_gain')

def mix_value(source):
    from math import isfinite
    new = any(k in source for k in _GAIN_FIELDS)
    if new and 'gain' in source:
        raise ValueError('Use either legacy gain or split bus gains, not both')
    if new:
        if 'master_gain' not in source:
            raise ValueError('Split VST mix requires explicit master_gain')
        value = {k:source.get(k, 1.) for k in _GAIN_FIELDS}
    else:
        value = dict(gain=source.get('gain', 0.))
    for name, number in value.items():
        limit = VST_MIDI_GAIN_MAX if name == 'midi_gain' else 1.
        if type(number) not in (int,float) or not 0 <= number <= limit or not isfinite(number):
            raise ValueError('Invalid VST gain')
    value['muted'] = source.get('muted', True)
    value['metronome'] = source.get('metronome', False)
    if type(value['muted']) is not bool or type(value['metronome']) is not bool:
        raise ValueError('Invalid VST mix switch')
    return value


def audition_rate_value(value):
    if type(value) not in (int, float) or not .25 <= value <= 2.:
        raise ValueError('试听速度应为 25% 至 200%。')
    return round(value * VST_RATE_UNIT) / VST_RATE_UNIT


class _RateChange:
    """Identity marker, separate from play/pause and timeline receipts."""


class _NotesMotion:
    """Retain the current control intent until the rendered clock observes it."""
    __slots__ = ('applied_intent',)

    def __init__(self):
        self.applied_intent = None


class _NotesSeek(_NotesMotion):
    """The locator survives a receipt that outruns its rendered clock."""
    __slots__ = ('wait_for_render',)

    def __init__(self, wait_for_render=True):
        super().__init__()
        self.wait_for_render = wait_for_render


class VstRoute:
    def __init__(self, project, instrument, libraries, device, submit, *, restore=True,
                 factory=PluginSession, projector=None, now=perf_counter, instrument_state=None, effects=None):
        self.project, self.instrument, self.libraries = project, instrument, libraries
        self.device, self.rate = dict(device), device['sample_rate']
        self.mix = mix_value(device)
        self.device.update(self.mix)
        self.projection_warnings = []
        self.instrument_state = instrument_state
        self.effect_catalog = effects or {}
        self.rack_restored = False
        self.loaded_legacy_state = None
        self.submit, self.factory, self.now, self.restore = submit, factory, now, restore
        self.projector = projector or Projector()
        self.epoch = uuid4().hex
        self.session = self.flight = self.conversion = self.newest = self.timeline = None
        self.replay_snapshot = None
        self._insert_flight = None
        self.wanted = self.submitted = self.play = self.motion = self.voice = None
        self.commands, self.awaiting = deque(), {}
        self.receipt_acks = set()
        self.lane_id = None
        self.duration = 0.
        self.requested_rate = 1.
        self.rate_request = self.rate_error = None
        self.loop = None
        self.pcm_source = None
        self.pcm_error = None
        self.wire = {}
        self._editor_open = False
        self._editor_revision = -1
        self._insert_editors = {}
        self._pending_insert_editors = set()
        self.next_status = self.intent = self.burst = 0
        self.open_attempted = self.retiring = self.released = self.confirmed = False
        self.close_step = 0
        self.error = self.projection_error = self.notice = None

    def begin_score_source(self):
        self._score_source = True
        self._score_resequence_notes = True
        self._restore_notes_seek = self._score_seek = self._requested_seek = None
        self.transport('pause')
        self.set_loop(0.,0.,False)
        self.set_pcm(None)

    def offer_score_source(self, score):
        from .vst_score_snapshot import WireScore
        if not getattr(self,'_score_source',False) or self.retiring:
            raise ValueError('No active piano score transport')
        self._offer_wire(WireScore(self.project, max(-1,self.wanted if self.wanted is not None else -1)+1, score))

    def seek_score_source(self, position):
        # A previous Notes/Score timeline can still be applied. Seeking it
        # before this Score receipt can snap to that timeline's shorter end.
        sample_of(position,self.rate)
        self._score_seek = (self.wanted, position)

    def cancel_score_seek(self):
        self._score_seek = None

    def _send_pending_source_seek(self):
        attribute='_score_seek' if getattr(self,'_score_source',False) else '_restore_notes_seek'
        pending=getattr(self,attribute,None)
        if pending is None:
            return False
        if ((attribute=='_score_seek' and pending[0]!=self.wanted)
                or self.wanted is None or pending[0]>self.wanted or self.projection_error):
            setattr(self,attribute,None)
            return False
        if self.applied is None or self.applied!=self.wanted:
            return False
        setattr(self,attribute,None)
        marker = _NotesSeek(wait_for_render=attribute != '_score_seek')
        self._requested_seek = marker, pending[1]
        self._send_transport(dict(op='seek',position=pending[1]),marker)
        return True

    def leave_score_source(self, frozen):
        if getattr(self,'_score_source',False) and not self.retiring:
            self.transport('pause')
            self._score_source = False
            self._requested_seek = None
            self.cancel_score_seek()
            self.offer(frozen)

    def offer(self, frozen):
        if getattr(self,'_score_source',False):
            return  # Notes publication remains frozen while score owns the rack
        from .vst_score_snapshot import WireNotes, WireReviewNotes, WireScore
        if isinstance(frozen, WireScore):
            # Active Score playback does not support replacing the rack.
            return
        if isinstance(frozen, (WireNotes,WireReviewNotes)):
            self._score_resequence_notes = True
        if getattr(self,'_score_resequence_notes',False):
            natural=frozen.head['applied_sequence']
            if self.wanted is not None and natural<=self.wanted:
                frozen=WireNotes(frozen,self.wanted+1)
        self._offer_wire(frozen)

    def _offer_wire(self, frozen):
        head = frozen.head
        sequence = head['applied_sequence']
        if head['project_id'] != self.project or type(sequence) is not int or not 0 <= sequence < 2**64:
            raise ValueError('VST 冻结快照不属于当前工程。')
        if self.retiring or self.wanted is not None and sequence <= self.wanted:
            return
        self.wanted, self.duration = sequence, head['duration']
        self.replay_snapshot = frozen
        self.newest, self.timeline, self.projection_error = (sequence, frozen), None, None
        # A task already admitted to IPC is ordered before any newer submission.
        # Cancel work that is still only waiting for an executor turn.
        if self.flight:
            kind, args, marker, f = self.flight
            if kind == 'rt_publish' or kind == 'rt_transport' and args['command']['op'] == 'play':
                if f.cancel() and kind == 'rt_transport' and self.motion == (marker, True):
                    self.play = marker

    def _project(self):
        if self.conversion and self.conversion[1].done():
            seq, f = self.conversion
            self.conversion = None
            if seq == self.wanted:
                try:
                    value = f.result()
                    if (value['sequence'], value['epoch'], value['project_id']) != (seq, self.epoch, self.project):
                        raise ValueError('VST 投影身份不匹配。')
                    self.projection_warnings = value.pop('warnings', [])
                    self.timeline = value
                except Exception as exc:
                    self._bad_projection(str(exc) or type(exc).__name__)
        if self.conversion is None and self.newest is not None:
            seq, frozen = self.newest
            self.newest = None
            from .vst_score_snapshot import WireScore
            if isinstance(frozen, WireScore):
                self.conversion = seq, self.submit(frozen.project_wire, self.projector, frozen, self.epoch, self.rate)
            else:
                self.conversion = seq, self.submit(self.projector, frozen, self.epoch, self.rate)

    def _bad_projection(self, error):
        self.projection_error = error
        if self.play is not None:
            self.motion = self.play = None

    @property
    def ready(self):
        return (not self.retiring and self.wire.get('phase') == 'ready'
                and self.wire.get('realtime_ready') is True
                and (getattr(self, 'saved_reopen', None) is None or getattr(self, 'saved_rack_confirmed', False)))

    @property
    def applied(self):
        seq = self.wire.get('applied_sequence') if self.wire.get('epoch') == self.epoch else None
        return seq if type(seq) is int else None

    @property
    def playing(self):
        return self.ready and self.applied is not None and self.wire['transport']['state'] == 'playing'

    @property
    def counting(self):
        return self.ready and self.applied is not None and self.wire['transport']['state']=='counting'

    @property
    def wants_play(self):
        return self.motion[1] if self.motion is not None else self.playing or self.counting

    @property
    def position(self):
        if self.wire.get('epoch') != self.epoch:
            return 0.
        transport = self.wire['transport']
        return (transport['position_samples'] + transport.get('position_fraction', 0) / VST_RATE_UNIT) / self.rate

    @property
    def requested_position(self):
        pending = getattr(self, '_requested_seek', None)
        return pending[1] if pending is not None else self.position

    @property
    def playback_rate(self):
        return self.wire['transport'].get('rate', 1.) if self.wire.get('epoch') == self.epoch else 1.

    @property
    def sampled_qpc_sec(self):
        return self.wire['transport'].get('sampled_qpc_sec') if self.wire.get('epoch') == self.epoch else None

    @property
    def score_rate_supported(self):
        return bool((self.wire.get('capabilities') or {}).get('score_rate'))

    @property
    def restoring_notes_rate(self):
        return getattr(self,'_notes_rate_restore',None) is not None

    def restore_notes_rate(self, value):
        self.commands=deque(row for row in self.commands
                            if row[0]!='rt_transport' or row[1].get('op')!='score_rate')
        self.set_rate(value,restore=self.score_rate_supported)

    def set_rate(self, value, *, score=False, restore=False):
        wide=score and self.score_rate_supported and getattr(self,'_score_source',False)
        if wide:
            if type(value) not in (int,float) or not .1<=value<=3.:
                raise ValueError('Score speed must be between 10% and 300%')
            value=round(value*VST_RATE_UNIT)/VST_RATE_UNIT
        else:
            value=audition_rate_value(value)
        if self.retiring or value == self.requested_rate and not restore:
            return
        if self.pcm_source is not None and value != 1.:
            raise ValueError('原曲与 MIDI 对比只支持原速。')
        self.requested_rate, self.rate_error = value, None
        self.rate_request = _RateChange()
        if restore or self.restoring_notes_rate:
            self._notes_rate_restore=(self.rate_request,value,False)
        packet = ('rt_transport', dict(op='score_rate' if wide else 'rate', rate=value), self.rate_request)
        # Coalesce adjacent, unsent changes, preserving seek/play command order.
        if self.commands and isinstance(self.commands[-1][2], _RateChange):
            self.commands[-1] = packet
        else:
            self.commands.append(packet)

    def _rate_result(self, marker, error):
        if marker is self.rate_request:
            self.rate_request = None
            self.rate_error = error
            restoring=getattr(self,'_notes_rate_restore',None)
            if restoring and restoring[0] is marker:
                if error:
                    self.fail(error)  # never start or rewrite Notes using an out-of-range Score speed
                else:
                    self._notes_rate_restore=(marker,restoring[1],True)
            elif error:
                self.requested_rate = self.playback_rate

    def set_mix(self, gain, muted=None):
        value = mix_value(gain if isinstance(gain,dict) else dict(gain=gain,muted=muted))
        if value == self.mix or self.retiring:
            return
        self.mix = value
        if not self.open_attempted:
            for key in ('gain', *_GAIN_FIELDS, 'muted', 'metronome'):
                self.device.pop(key,None)
            self.device.update(value)
        elif self.commands and self.commands[-1][0] == 'rt_mix':
            # Absolute gains: replace only adjacent unsent mixes. Transport and
            # audition commands between changes retain their original order.
            self.commands[-1] = ('rt_mix',value,None)
        else:
            self.commands.append(('rt_mix',value,None))

    @property
    def pcm_ready(self):
        value = self.wire.get('pcm') or {}
        source = self.pcm_source
        if source is None:
            return value.get('state','off') == 'off'
        return (value.get('state') == 'ready' and value.get('epoch') == self.epoch
                and all(value.get(k) == source[k] for k in ('key','path','identity','offset_seconds')))

    def set_pcm(self, source):
        from copy import deepcopy
        if self.retiring or source == self.pcm_source and not self.pcm_error:
            return
        if source is not None and self.requested_rate != 1.:
            raise ValueError('原曲与 MIDI 对比只支持原速。')
        self.pcm_source = deepcopy(source)
        self.pcm_error = None
        # Coalesce only unsent source requests; in-flight worker ownership is ordered.
        self.commands = deque(row for row in self.commands if row[0] != 'rt_pcm')
        self.commands.append(('rt_pcm',dict(source=deepcopy(source),epoch=self.epoch),None))

    def set_loop(self, start, end, enabled):
        sample_of(start, self.rate)
        sample_of(end, self.rate)
        if type(enabled) is not bool:
            raise ValueError('无效的 VST 循环设置。')
        value = (start, end, enabled)
        if value != self.loop and not self.retiring:
            self.loop = value
            self.commands.append(('rt_transport', dict(op='loop', bounds=value), None))

    def transport(self, op, position=None, *, count_in=None, track_notes_seek=False):
        if self.retiring:
            return
        if op == 'play' and self.projection_error:
            raise ValueError(self.projection_error)
        if op not in ('play', 'stop', 'pause', 'seek'):
            raise ValueError('无效的 VST 走带操作。')
        marker = None
        if op in ('stop', 'pause', 'seek'):
            if self.play is not None:
                if self.motion == (self.play, True):
                    self.motion = None
                self.play = None
            if self.flight and self.flight[0] == 'rt_transport' and self.flight[1]['command']['op'] == 'play':
                if self.flight[3].cancel() and self.motion == (self.flight[2], True):
                    self.motion = None
        if op == 'stop' and getattr(self, '_requested_seek', None) is not None:
            self._requested_seek = None
        if op != 'seek':
            marker = object() if getattr(self, '_score_source', False) else _NotesMotion()
            self.motion = marker, op == 'play'
        if op == 'play':
            self.play = marker
            self._count_play=(marker,count_in)
        else:
            payload = dict(op=op)
            if op == 'seek':
                sample_of(position, self.rate)
                payload['position'] = position
                if track_notes_seek:
                    # An explicit Notes locator supersedes a deferred return from Score.
                    self._restore_notes_seek = None
                    marker = _NotesSeek()
                    self._requested_seek = marker, position
            self.commands.append(('rt_transport', payload, marker))

    def _release_voice(self, clear=False):
        if self.voice is not None:
            previous, _ = self.voice
            self.voice = None
            value = dict(op='clear') if clear else dict(previous, op='off', velocity=0)
            self.commands.append(('rt_audition', value, None))

    def clear(self):
        self._release_voice(clear=True)

    def audition(self, command):
        op = command.get('op')
        if self.retiring or command.get('epoch') != self.epoch:
            return
        if op == 'clear':
            self.clear()
            return
        token = command.get('token')
        if not isinstance(token, str) or not 0 < len(token) <= 128:
            return
        if op == 'off':
            if self.voice and self.voice[0]['token'] == token:
                self._release_voice()
            return
        if op != 'on' or not self.ready or self.applied is None:
            return
        p, c, v = (command.get(k) for k in ('pitch', 'channel', 'velocity'))
        if not (type(p) is type(c) is type(v) is int and 0 <= p < 128 and 0 <= c < 16 and 1 <= v < 128):
            return
        if self.voice and self.voice[0]['token'] == token:
            return
        self._release_voice()
        if len(self.commands) >= 64:
            self.notice = 'VST 试音输入过密；本次未发音。'
            return
        value = dict(op='on', token=token, pitch=p, channel=c, velocity=v)
        self.voice = value, self.now()+.18
        self.commands.append(('rt_audition', value, None))

    _EDITOR_ACTIONS = ('editor_open', 'editor_close')

    @property
    def editor_open(self):
        return self.ready and self._editor_open

    @property
    def editor_pending(self):
        return bool((self.flight and self.flight[0] in self._EDITOR_ACTIONS) or
                    any(row[0] in self._EDITOR_ACTIONS for row in self.commands))

    def insert_editor_pending(self, sid):
        return sid in self._pending_insert_editors

    def queue_insert_editor(self, args):
        sid=args['slot_id']
        if sid not in self._pending_insert_editors:
            self._pending_insert_editors.add(sid)
            self.commands.append(('insert_editor',args,None))

    def insert_editor_open(self, sid, key):
        return bool(self.ready and key and self._insert_editors.get(sid) == key)

    def _observe_editors(self, state):
        if not isinstance(state, dict): return
        revision = state.get('revision')
        if type(revision) is not int or revision <= self._editor_revision: return
        slots = state.get('inserts')
        if type(state.get('instrument')) is not bool or not isinstance(slots,dict): return
        self._editor_revision = revision
        self._editor_open = state['instrument']
        self._insert_editors = {sid:key for sid,key in slots.items() if isinstance(sid,str) and isinstance(key,str)}

    def plugin_action(self, command):
        if command not in (*self._EDITOR_ACTIONS, 'save_instrument', 'save_default_patch') or not self.ready:
            raise ValueError('VST 乐器尚不可用。')
        if command in self._EDITOR_ACTIONS and self.editor_pending:
            return
        if self.flight and self.flight[0] == command or any(row[0] == command for row in self.commands):
            return
        self.commands.append((command, {}, None))

    def capture_state(self):
        result = Future()
        if not self.ready or self.session is None:
            result.set_exception(RuntimeError('VST instrument is not ready for state capture'))
            return result
        self.commands.append(('capture_state', {}, result))
        return result

    def retire(self):
        if self.retiring:
            return
        self.retiring = True
        self.retire_started=self.now()
        first_frames = getattr(self, 'first_driver_frames', None)
        if first_frames is not None:
            first_frames.cancel()
        self.replay_snapshot = None
        self.newest = self.timeline = self.play = self.motion = self.voice = None
        for kind, _, marker in self.commands:
            if kind in ('capture_state','insert_publish','insert_action') and marker is not None and not marker.done():
                marker.set_exception(RuntimeError('VST route retired before state capture'))
        self.commands.clear()
        self._pending_insert_editors.clear()
        if self.conversion:
            self.conversion[1].cancel()
            self.conversion = None
        if self.flight and self.flight[0] not in ('rt_open', 'load', 'capture_state'):
            self.flight[3].cancel()

    def fail(self, error, *, stage=None):
        if self.error is None:
            self.error = str(error) or type(error).__name__
            self.failure_stage = stage or getattr(error, 'category', None) or 'route'
        self.retire()

    def _load(self):
        from pathlib import Path
        if not Path(self.instrument.module).exists():
            raise FileNotFoundError('找不到已配置的 VST 乐器：' + self.instrument.name)
        from astra.vst_host.session import SetupProcessingRejected
        try:
            native_setup=getattr(self.factory,'instrument_start_contract',None)==1
            n=self.device.get('buffer_frames') if isinstance(self.device,dict) else None
            hint=getattr(self,'instrument_block_proof',None)
            capacity=16384
            first_frames = getattr(self, 'first_driver_frames', None)
            if native_setup:
                if hint:
                    capacity = hint[2]
                if first_frames is not None or getattr(self, 'prepare_device', None):
                    # This also applies while a different output owner is playing.
                    # Only the independent renderer is prepared at this point.
                    capacity = min(hint[2] if hint else (n or 1024), 1024)
            args=dict(instrument_setup=(0 if hint else (n or 0),hint[2] if hint else 0)) if native_setup else {}
            if native_setup and first_frames is not None:
                args['instrument_setup'] = (0, 0)
            if getattr(self, 'instrument_standby', None) is not None and self.factory is PluginSession:
                args['standby'] = self.instrument_standby
            session = self.factory(self.libraries.library, self.instrument.module, self.instrument.class_id,
                                   sample_rate=self.rate, max_block=capacity,**args)
        except SetupProcessingRejected:
            if native_setup:
                if first_frames is None:
                    raise
                # Shared endpoint observation resolves this on a callback, or
                # route retirement cancels it. Do not race the driver's own open.
                retry_capacity = first_frames.result()
                if self.retiring:
                    raise CancelledError('Instrument selection was canceled')
                if retry_capacity == capacity:
                    raise
            else:
                if type(n) is not int or not 0 < n <= 1024:
                    raise
                retry_capacity = 1024
            session = self.factory(self.libraries.library, self.instrument.module, self.instrument.class_id,
                                   sample_rate=self.rate, max_block=retry_capacity, **args)
        try:
            initial = (getattr(session, 'info', None) or {}).get('realtime')
            status = self._check_status(initial if initial is not None else session.request('rt_status'))
            if status['phase'] != 'closed' or status['realtime_ready']:
                raise RuntimeError('VST 工作进程未处于关闭的初始输出状态。')
            if self.restore:
                saved = self._instrument_image()
                matched = False
                if saved is None:
                    legacy = restore_instrument(session, self.project, self.instrument)
                    if legacy is not None:
                        matched = True
                        from astra.live_owner import prepare_instrument
                        self.loaded_legacy_state = prepare_instrument(legacy)
                elif (saved.get('instrument') == self.instrument.key and
                      saved.get('class_id') == self.instrument.class_id.lower()):
                    restore_initial_state(session, saved['state'])
                    self._loaded_attachment = self.instrument_state
                    matched = True
                    decoded = getattr(self, '_decoded_rack', None)
                    rack_slots = decoded[1]['slots'] if decoded and decoded[1] else ()
                    if (getattr(self, 'saved_reopen', None) is not None and not rack_slots
                            and getattr(self, 'live_insert_source', None) is None
                            and getattr(self, 'prepare_candidate_insert', None) is None):
                        self.restored_instrument_identity = (self.instrument.key, self.instrument.class_id.lower())
                        self.rack_restored = True
                        if hasattr(session, 'initial_image'):
                            session.initial_image = None
                if not matched:
                    from .vst_project import restore_default_patch
                    restore_default_patch(session, self.instrument)
        except BaseException:
            session.close()  # No rt_open was issued on this session.
            raise
        return session

    def _instrument_image(self):
        from astra.vst_host.rack_state import from_attachment
        if self.instrument_state is None:
            return None
        prepared = getattr(self,'saved_reopen_rack',None)
        self.saved_reopen_rack = None
        rack = prepared if prepared is not None else from_attachment(self.instrument_state)
        self._decoded_rack = self.instrument_state, rack
        plugin = rack['instrument']
        return dict(instrument=plugin['key'],class_id=plugin['class_id'],state=plugin['state']) if plugin else {'disabled':True}

    def _restore_rack(self):
        import base64
        from astra.vst_host.rack_state import from_attachment,encode
        previous, self._decoded_rack = getattr(self, '_decoded_rack', (None, None)), None
        prior = previous[0]
        saved = self.instrument_state
        # Owner.instrument_state returns a fresh envelope for the same immutable
        # blob. The first-load handoff often fetches it twice.
        same_image = prior is saved or (prior and saved and prior.get('state') is saved.get('state')
                      and all(prior.get(k) == saved.get(k) for k in ('instrument','class_id','disabled')))
        original = previous[1] if previous[1] is not None and same_image else from_attachment(saved)
        rack = dict(original)
        image = rack['instrument']
        self.restored_instrument_identity = (image['key'], image['class_id']) if image else (None, None)
        confirmed = getattr(self.session, 'initial_image', None)
        if (self.restore and image and image['key']==self.instrument.key and
                image['class_id']==self.instrument.class_id.lower() and
                self.instrument_state is not getattr(self, '_loaded_attachment', None) and
                (confirmed is None or confirmed[1] != image['state'])):
            restore_initial_state(self.session, image['state'])
        self._loaded_attachment = None
        source = getattr(self, 'live_insert_source', None)
        if source is not None:
            from astra.vst_host.rack_state import decode
            snapshot = source.request('snapshot_live_inserts')
            rack['slots'] = decode(base64.b64decode(snapshot['rack'], validate=True))['slots']
            self.live_insert_generation = snapshot['generation']
        if rack['slots']:
            insert_image = dict(version=1,instrument=None,slots=rack['slots'])
            reply = self.session.request('insert_publish',rack=base64.b64encode(encode(insert_image)).decode('ascii'),
                                         catalog=self._catalog_for_slots(rack['slots']))
            if getattr(self, 'saved_reopen', None) is not None and (reply or {}).get('accepted') is not True:
                raise RuntimeError('插件未接受已保存的效果器机架。')
        if getattr(self, 'prepare_candidate_insert', None):
            if rack['slots']:
                # Apply the restored rack's pending native revision. Without a
                # callback yet, a second publication would wait on itself.
                self.session.request('candidate_warm_rack')
            self.prepare_candidate_insert()
        elif getattr(self, 'prepare_device', None) and getattr(self, 'saved_reopen', None) is None:
            self.prepared_capture = capture_state(self.session, self.instrument, initial=True,
                                                  prior_attachment=saved, prior_rack=original)
        if hasattr(self.session, 'initial_image'):
            self.session.initial_image = None

    def insert_action(self, action):
        future=Future()
        if self.retiring or not self.ready:
            future.set_exception(RuntimeError('Instrument route is not ready'))
            return future
        self.commands.append(('insert_action',dict(action,catalog=self.effect_catalog),future))
        return future

    def _submit_insert_action(self, args):
        # Do not park the audio lane on every edit. CtrlS/recovery/departure
        # force the real state capture through the existing exclusive lease.
        return self.session.request('insert_action',**args)

    def _catalog_for_slots(self, slots):
        keys = {slot['plugin']['key'] for slot in slots if slot['plugin']}
        return {key:self.effect_catalog[key] for key in keys if key in self.effect_catalog}

    def insert_request(self, slots):
        import base64
        from astra.vst_host.rack_state import encode
        future=Future()
        if self.retiring or not self.ready:
            future.set_exception(RuntimeError('Instrument route is not ready'))
            return future
        rack=dict(version=1,instrument=None,slots=slots)
        self.commands.append(('insert_publish',dict(rack=base64.b64encode(encode(rack)).decode('ascii'),
                                                    catalog=self._catalog_for_slots(slots)),future))
        return future

    def _post(self, kind, args=None, marker=None):
        assert self.flight is None
        args = args or {}
        started = self.now()
        if kind == 'rt_status' and self.lane_id is not None:
            args = dict(args, lane_id=self.lane_id, ack_receipts=sorted(self.receipt_acks),
                        known_editor_revision=self._editor_revision)
        if kind == 'load':
            task = self.submit(self._load)
        elif kind == 'dispose':
            task = self.submit(self.session.close)
        elif kind == 'insert_action':
            task = self.submit(self._submit_insert_action,args)
        elif kind == 'restore_rack':
            task = self.submit(self._restore_rack)
        elif kind == 'capture_state':
            task = self.submit(capture_state, self.session, self.instrument)
        elif kind == 'save_instrument':
            task = self.submit(capture_instrument, self.session, self.project, self.instrument)
        elif kind == 'save_default_patch':
            from .vst_project import save_default_patch
            task = self.submit(save_default_patch, self.session, self.instrument)
        else:
            task = self.submit(self.session.request, kind, **args)
        if kind=='insert_action' and args.get('action')=='insert_add' and self.wire.get('insert_scoped_transport') is True:
            assert self._insert_flight is None
            self._insert_flight = kind, args, marker, task
            self.insert_flight_started = started
        else:
            self.flight = kind, args, marker, task
            self.flight_started = started

    @staticmethod
    def _check_status(s):
        if (not isinstance(s, dict) or s.get('version') != 1 or type(s.get('realtime_ready')) is not bool or
                s.get('phase') not in ('closed', 'opening', 'ready', 'closing', 'error')):
            raise RuntimeError('VST 工作进程未提供 RT v1 状态。')
        return s

    def _status(self, result):
        s = self._check_status(result)
        lane = s.get('lane_id')
        if lane is not None:
            if self.lane_id is not None and lane != self.lane_id:
                raise RuntimeError('VST worker lane changed during a route')
            self.lane_id = lane
        if self.retiring:
            if self.close_step == 2 and s['phase'] == 'closed' and not s['realtime_ready']:
                self.confirmed = True
            elif s.get('last_error') and self.close_step == 2:
                self.notice = str(s['last_error'])
            return
        if s['phase'] in ('closed', 'error', 'closing'):
            raise RuntimeError(s.get('last_error') or 'VST 输出未能保持打开。')
        if s['phase'] == 'ready' and s['realtime_ready']:
            d, t = s['device'], s['transport']
            if (any(d[k] != self.device[k] for k in ('backend', 'device_id', 'output_channels', 'sample_rate')) or
                    type(d['buffer_frames']) is not int or not 1 <= d['buffer_frames'] <= 16384):
                raise RuntimeError('VST 实际设备、通道或处理参数与所选路由不兼容。没有改用其他输出。')
            fraction = t.get('position_fraction', 0)
            rate = t.get('rate', 1.)
            wide=bool((s.get('capabilities') or {}).get('score_rate')) and (
                getattr(self,'_score_source',False) or self.restoring_notes_rate)
            minimum,maximum=(.1,3.) if wide else (.25,2.)
            if (type(t['position_samples']) is not int or t['position_samples'] < 0 or
                    type(fraction) is not int or not 0 <= fraction < VST_RATE_UNIT or
                    type(rate) not in (int, float) or not minimum <= rate <= maximum or
                    t['state'] not in ('playing', 'paused', 'stopped', 'counting') or
                    (t['state']=='counting' and not (s.get('capabilities') or {}).get('score_count_in'))):
                raise RuntimeError('VST 渲染位置无效。')
            reopen = getattr(self,'saved_reopen',None)
            if reopen is not None and not getattr(self,'saved_rack_confirmed',False):
                inserts = s.get('inserts') or {}
                from .vst_saved_restore import slot_signature
                actual = slot_signature(inserts.get('slots', ()))
                if self.saved_reopen_slots != actual or any(x.get('missing') for x in inserts.get('slots', ())):
                    raise RuntimeError('效果器机架未完整恢复，已保留原保存状态。')
                if not inserts.get('pending_revision'):
                    self.saved_rack_confirmed = True
        if s.get('epoch') not in (None, '', self.epoch):
            raise RuntimeError('VST 输出返回了其他路由的状态。')
        self.wire = s
        self.last_worker_status_at = self.now()
        if s.get('editor_state') is not None:
            self._observe_editors(s['editor_state'])
        elif self._editor_revision < 0:  # older worker/fixture without the window protocol
            self._editor_open = s.get('epoch') == self.epoch and s.get('editor_open') is True
        if s.get('epoch') != self.epoch:
            return
        for receipt in s.get('receipts', ()):
            if receipt.get('status') in ('applied', 'stale', 'failed'):
                self.receipt_acks.add(receipt['command_id'])
            prior = self.awaiting.get(receipt['command_id'])
            if prior is None:
                continue
            kind, seq, intent, marker = prior
            outcome = receipt['status']
            if outcome not in ('applied', 'stale', 'failed'):
                continue
            if kind == 'rt_transport' and receipt.get('intent') != intent:
                raise RuntimeError('VST 走带回执身份不匹配。')
            del self.awaiting[receipt['command_id']]
            if isinstance(marker, _NotesSeek):
                if outcome == 'applied' and type(intent) is int and marker.wait_for_render:
                    marker.applied_intent = intent
                else:
                    self._clear_requested_seek(marker)
            if isinstance(marker, _RateChange):
                self._rate_result(marker, None if outcome == 'applied' else '未应用 VST 试听速度；已保留实际设置。')
                continue
            if self.motion is not None and self.motion[0] is marker:
                if outcome == 'applied' and type(intent) is int and isinstance(marker, _NotesMotion):
                    marker.applied_intent = intent
                else:
                    self.motion = None
            if outcome != 'applied':
                message = s.get('last_error') or f'{kind}: {outcome} ({receipt["command_id"]})'
                if kind == 'rt_audition':
                    self.fail(message)  # If release failed, retire the audition lane; never send a timeline panic.
                elif kind == 'rt_publish' and seq == self.wanted:
                    self._bad_projection(message)
                elif kind == 'rt_transport':
                    self.notice = message
        # Native RT publishes outcomes before it publishes the render view.
        # A matching receipt with the previous view must not resurrect its
        # cursor or playing bit. The epoch and exact marker still fence old
        # commands; a newer render watermark may cover several applied ones.
        render_epoch = s.get('render_epoch')
        rendered = (s.get('transport') or {}).get('last_intent') if render_epoch in (None, self.epoch) else None
        if type(rendered) is int:
            pending = getattr(self, '_requested_seek', None)
            if pending is not None:
                seen = pending[0].applied_intent
                if seen is not None and rendered >= seen:
                    # This locator generation belongs to the rendered position,
                    # not a requested or merely acknowledged transport intent.
                    self._recording_locate_generation = getattr(self, '_recording_locate_generation', 0) + 1
                    self._clear_requested_seek(pending[0])
            motion = self.motion
            if motion is not None and isinstance(motion[0], _NotesMotion):
                seen = motion[0].applied_intent
                if seen is not None and rendered >= seen:
                    self.motion = None
        restoring=getattr(self,'_notes_rate_restore',None)
        if restoring and restoring[2] and self.playback_rate==restoring[1]:
            self._notes_rate_restore=None
        # A coalesced earlier timeline need not occupy product memory forever.
        if self.applied is not None:
            self.awaiting = {ident: x for ident, x in self.awaiting.items()
                             if x[0] != 'rt_publish' or x[1] > self.applied}

    def _finish(self):
        if self.flight is None and self._insert_flight is not None and self._insert_flight[3].done():
            self.flight, self._insert_flight = self._insert_flight, None
        if self.flight is None or not self.flight[3].done():
            return
        kind, args, marker, task = self.flight
        self.flight = None
        if kind == 'insert_editor':
            self._pending_insert_editors.discard(args.get('slot_id'))
        try:
            value = task.result()
            if kind == 'load':
                self.session = value
            elif kind == 'park_navigation':
                if not isinstance(value,dict):
                    raise RuntimeError('Missing navigation rack receipt')
                self.navigation_receipt=value
                self.parked_session=self.session
                self.session,self.released=None,True
            elif kind == 'standby_instrument':
                if value is True:
                    self.parked_session = self.session
                    self.session, self.released = None, True
                else:
                    self.standby_binding = None
            elif kind == 'dispose':
                self.session, self.released = None, True
            elif kind == 'rt_status':
                self._status(value)
                if args.get('lane_id') == self.lane_id:
                    self.receipt_acks.difference_update(args.get('ack_receipts', ()))
            elif kind == 'rt_pcm':
                if not isinstance(value,dict) or value.get('accepted') is not True or value.get('epoch') != self.epoch:
                    raise RuntimeError('PCM source request was not accepted')
            elif kind == 'restore_rack':
                self.rack_restored = True
            elif kind in ('insert_publish','insert_action'):
                if kind=='insert_action':
                    self.wire['inserts']=value['inserts']
                    self.wire['state_generation']=value['state_generation']
                if marker is not None and not marker.done():marker.set_result(value)
            elif kind == 'capture_state':
                if not marker.done():
                    marker.set_result(value)
            elif not self.retiring:
                if kind in ('rt_publish', 'rt_audition', 'rt_transport'):
                    packet = args['timeline'] if kind == 'rt_publish' else args['command']
                    seq, intent = packet.get('sequence'), packet.get('intent')
                    if (not isinstance(value, dict) or value.get('accepted') is not True or
                            value.get('epoch') != self.epoch or type(value.get('command_id')) is not int or
                            seq is not None and kind == 'rt_publish' and value.get('sequence') != seq):
                        raise RuntimeError(f'VST 未接受 {kind}。')
                    ident = value['command_id']
                    if ident in self.awaiting:
                        raise RuntimeError('VST 重复使用了尚未完成的命令编号。')
                    self.awaiting[ident] = kind, seq, intent, marker
                    if kind == 'rt_publish':
                        self.submitted = seq
                    if kind in ('rt_publish', 'rt_transport'):
                        self.next_status = 0
                elif kind == 'rt_open':
                    self.next_status = 0
                elif kind in (*self._EDITOR_ACTIONS, 'insert_editor'):
                    if isinstance(value,dict) and 'editor_lane_id' in value:
                        if value['editor_lane_id'] != self.lane_id: return
                        self._observe_editors(value.get('editor_state'))
                    elif kind in self._EDITOR_ACTIONS and self._editor_revision < 0:
                        self._editor_open = bool(value and value.get('editor_open') is True)
                    self.notice = (value or {}).get('error')
                    if not self.notice and not (value or {}).get('skipped') and (value or {}).get('available') is False:
                        self.notice = '此插件未提供面板。'
                elif kind == 'save_instrument':
                    self.notice = '已保存项目乐器状态。'
                elif kind == 'save_default_patch':
                    self.notice = '已设为此乐器在本机的初始音色。'
        except CancelledError:
            if isinstance(marker, _NotesSeek):
                self._clear_requested_seek(marker)
            if kind in ('capture_state','insert_publish','insert_action') and marker is not None and not marker.done():
                marker.set_exception(RuntimeError('VST state capture cancelled'))
        except Exception as exc:
            if isinstance(marker, _NotesSeek):
                self._clear_requested_seek(marker)
            text = str(exc) or type(exc).__name__
            if kind in ('capture_state','insert_publish','insert_action'):
                if marker is not None and not marker.done():
                    marker.set_exception(exc)
            elif self.retiring:
                self.notice = 'VST 释放尚未确认：' + text
                if kind == 'park_navigation':
                    self.navigation_park=None
                if kind == 'standby_instrument':
                    self.standby_binding = None
                if kind == 'dispose':
                    self.close_step = 3  # Do not spin calling close on a broken session.
            elif kind == 'rt_publish':
                if args['timeline']['sequence'] == self.wanted:
                    self._bad_projection(text)
            elif kind == 'rt_pcm':
                self.pcm_error = text
                self.notice = text
            elif kind == 'rt_transport' and isinstance(marker, _RateChange):
                self._rate_result(marker, '未应用 VST 试听速度：' + text)
            elif kind in (*self._EDITOR_ACTIONS, 'insert_editor', 'save_instrument', 'save_default_patch', 'rt_transport'):
                if kind in (*self._EDITOR_ACTIONS, 'insert_editor'):
                    # A failed operation is not evidence that a live window closed.
                    self.next_status = 0
                self.notice = text
                if self.motion is not None and self.motion[0] is marker:
                    self.motion = None
            else:
                stage = getattr(exc, 'category', None)
                if stage is None:
                    process = getattr(self.session, 'process', None)
                    stage = 'worker_exit' if process is not None and process.poll() is not None else kind
                self.fail(exc, stage=stage)

    def _close_tick(self, now):
        if self.session is None:
            self.released = True
        elif ('kept_endpoint' in self.device and getattr(self.session,'process',None) is not None
              and self.session.process.poll() is not None):
            # The worker owns only a mapped writer. Process exit releases its lease.
            self._post('dispose')
        elif self.close_step == 3:
            return
        elif self.confirmed and getattr(self,'navigation_park',None) is not None and not self.error:
            self._post('park_navigation')
        elif self.confirmed and getattr(self,'standby_binding',None) is not None and not self.error:
            self._post('standby_instrument')
        elif not self.open_attempted or self.confirmed:
            self._post('dispose')
        elif self.close_step == 0 and self.submitted is not None:
            self.close_step = 1
            self._post('rt_audition', dict(command=dict(op='clear', epoch=self.epoch)))
        elif self.close_step < 2:
            self.close_step, self.next_status = 2, 0
            self._post('rt_close')
        elif now >= self.next_status:
            self.next_status = now+.10
            self._post('rt_status')

    def pump(self, may_open=False):
        # A slow GUI tick must not spend every turn polling. After a completed
        # status, allow one ready command/projection before the next due poll.
        status_finished = bool(self.flight and self.flight[0] == 'rt_status' and self.flight[3].done())
        self._finish()
        if not self.retiring and self.session is not None:
            process = getattr(self.session, 'process', None)
            if process is not None and (code := process.poll()) is not None:
                failure = getattr(self.session, '_failure', None)
                category = getattr(failure, 'category', None)
                self.fail(failure if category else f'VST 工作进程已退出（{code}）',
                          stage=category or 'worker_exit')
        now = self.now()
        if (self.retiring and getattr(self,'prepare_device',None) and self.session and
                now-getattr(self,'retire_started',now)>3 and
                not getattr(self,'worker_reaper',None) and self.session.process.poll() is None):
            # The mapped writer never owns the physical stream. A blocked vendor
            # exit may be reaped even while an IPC future is draining.
            self.worker_reaper=self.submit(self.session.terminate)
        if not self.retiring:
            self._project()
            if self.voice and now >= self.voice[1]:
                self._release_voice()
        if self.flight is not None or self.released:
            return
        if self._insert_flight is not None:
            if not self.retiring:
                if now >= self.next_status and not status_finished:
                    self.next_status = now+.04
                    self._post('rt_status')
                    return
                eligible = next((i for i,row in enumerate(self.commands) if row[0]=='rt_transport'),None)
                if eligible is not None and self.applied is not None:
                    self.commands.rotate(-eligible)
                    _, command, marker = self.commands.popleft()
                    self.commands.rotate(eligible)
                    self._send_transport(command, marker)
                elif self._send_pending_source_seek():
                    pass
                elif self.play is not None and self.applied is not None and self.applied==self.wanted and self.pcm_ready and not self.restoring_notes_rate:
                    marker,self.play=self.play,None
                    self._send_play(marker)
            return
        if self.retiring:
            self._close_tick(now)
        elif self.session is None:
            if may_open:
                if getattr(self,'observe_capacity',None) and not self.observe_capacity(self):return
                self._post('load')
        elif not self.rack_restored and may_open:
            self._post('restore_rack')
        elif not self.open_attempted:
            if may_open:
                if getattr(self, 'prepare_device', None) and not self.prepare_device(self):
                    return
                self.open_attempted = True
                self._post('rt_open', dict(route=self.device, **self.libraries.open_arguments()))
        elif (now >= self.next_status or self.burst >= 8) and not (status_finished and self.ready):
            active_wait = (not self.ready or self.play is not None
                           or (self.wanted is not None and self.applied != self.wanted)
                           or self.motion is not None or getattr(self, '_requested_seek', None) is not None)
            cadence = .008 if active_wait else (.010 if getattr(self, 'recording_active', False) else .040)
            self.next_status, self.burst = now + cadence, 0
            self._post('rt_status')
        elif self.ready:
            if len(self.awaiting) >= 256:
                self.fail('VST 回执未能确认；已开始关闭输出。')
                return
            eligible = next((i for i, row in enumerate(self.commands)
                             if self.applied is not None or row[0] not in ('rt_transport', 'rt_audition')), None)
            if eligible is not None:
                self.commands.rotate(-eligible)
                kind, command, marker = self.commands.popleft()
                self.commands.rotate(eligible)
                self.burst += 1
                if kind == 'rt_transport':
                    self._send_transport(command, marker)
                elif kind == 'rt_audition':
                    self._post(kind, dict(command=dict(command, epoch=self.epoch)))
                elif kind == 'capture_state':
                    self._post(kind, marker=marker)
                elif kind == 'rt_mix':
                    self._post(kind, dict(mix=command))
                elif kind in ('rt_pcm','insert_publish','insert_editor','insert_action'):
                    self._post(kind, command, marker)
                else:
                    self._post(kind)
            elif self.timeline is not None:
                value, self.timeline = self.timeline, None
                self.burst += 1
                self._post('rt_publish', dict(timeline=value))
            elif self._send_pending_source_seek():
                pass
            elif self.play is not None and self.applied is not None and self.applied == self.wanted and self.pcm_ready and not self.restoring_notes_rate:
                marker, self.play = self.play, None
                self._send_play(marker)

    def _send_play(self, marker):
        value=dict(op='play',sequence=self.wanted)
        pending=getattr(self,'_count_play',None)
        if pending is not None and pending[0] is marker and pending[1] is not None:
            value['countIn']=pending[1]
        self._send_transport(value,marker)

    def _clear_requested_seek(self, marker):
        pending = getattr(self, '_requested_seek', None)
        if pending is not None and pending[0] is marker:
            self._requested_seek = None

    def _send_transport(self, value, marker):
        self.intent += 1
        value = dict(value, epoch=self.epoch, intent=self.intent)
        if value['op'] == 'seek':
            value['sample'] = sample_of(min(self.duration, value.pop('position')), self.rate)
        elif value['op'] == 'loop':
            left, right, enabled = value.pop('bounds')
            a, b = (sample_of(min(self.duration, t), self.rate) for t in (left, right))
            value['loop'] = dict(enabled=enabled and a < b, start_sample=min(a,b), end_sample=max(a,b))
        self._post('rt_transport', dict(command=value), marker)
