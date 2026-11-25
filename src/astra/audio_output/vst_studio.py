"""Bind the project VST route into Studio's existing output handoff."""
from collections import deque
from concurrent.futures import Future
from threading import Lock
from dataclasses import dataclass, replace
import json
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, QTimer
from PySide6.QtWidgets import QWidget

from astra import live_owner
from . import vst_saved_restore as saved_restore
from . import transport_pages
from .vst_project import device_route, setup_values, global_device_route
from .vst_route import VST_MIDI_GAIN_MAX, VstRoute, audition_rate_value


class _Work(QRunnable):
    def __init__(self, done, task, fn, args, kwargs):
        super().__init__()
        self.done, self.task, self.fn, self.args, self.kwargs = done, task, fn, args, kwargs

    def run(self):
        if self.task.set_running_or_notify_cancel():
            try:
                result = self.fn(*self.args, **self.kwargs)
            except BaseException as exc:
                self.task.set_exception(exc)
            else:
                self.task.set_result(result)
        try:
            self.done.emit()
        except RuntimeError:
            pass  # The bridge may be gone after an offline catalog read.


_runtime_log_lock = Lock()


class _ScoreOnlyOwner:
    """Capture sink for the built-in Score piano.

    The built-in MuseScore piano is the Score page's playback sound, not the
    document's instrument: it never reads, replaces or saves the editor's
    instrument attachment (which keeps whatever rack the person chose)."""
    _quiet = ('bind_instrument_route', 'mark_instrument_changed', 'update_instrument_state',
              'save_with_instrument', 'begin_instrument_capture', 'end_instrument_capture',
              'instrument_capture_failed')

    def __init__(self, owner):
        self._owner = owner

    def __getattr__(self, name):
        if name in _ScoreOnlyOwner._quiet:
            return lambda *args, **kwargs: None
        return getattr(self._owner, name)


class _Dispatch(QObject):
    done = Signal()

    def submit(self, fn, *args, **kwargs):
        task = Future()
        QThreadPool.globalInstance().start(_Work(self.done, task, fn, args, kwargs))
        return task


@dataclass(eq=False, frozen=True)
class _Binding:
    project: dict
    owner: object
    uri: str
    source: object
    native_epoch: int


class VstStudioRoute:
    # Score playback menu choices kept in the app's saved UI preferences, so
    # 预备拍 / 忽略谱面速度 survive a restart and apply from the first play.
    def _score_pref(self, key):
        try:
            from ..ui_preferences import saved_ui_preferences
            return bool(saved_ui_preferences(getattr(self, '_prefs', None) or {})[key])
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def _score_flat(self):
        flat = getattr(self, '_score_flat_tempo', None)
        return self._score_pref('score_flat_tempo') if flat is None else bool(flat)

    def _remember_score_pref(self, key, value):
        save = getattr(self, 'saveUiPreferences', None)
        if callable(save) and self._score_pref(key) != value:
            save(json.dumps({key: value}))

    def __init__(self, *args, vst_session_factory=None, **kwargs):
        self._vst = self._vst_binding = self._vst_choice = None
        self._instrument_required = True
        self._vst_retiring = []
        self._previous_instrument = None
        self._standby_memory_check = 0.
        self._plugin_scan = None
        self._plugin_scan_auto = False
        self._plugin_scan_retry = False
        self._plugin_startup_scheduled = False
        self._plugin_scan_status = {'running':False}
        self._vst_catalog = None
        self._vst_effect_catalog = {}
        from astra.studio_bridge import PREFERENCES
        self._favorite_path = PREFERENCES.with_name('vst-preferred-effect.json')
        try:
            saved = json.loads(self._favorite_path.read_text(encoding='utf8'))
            self._favorite_effect = saved if isinstance(saved,str) else None
        except (OSError,ValueError):
            self._favorite_effect = None
        self._vst_effect_failures = {}
        self._vst_display_catalog = None
        self._vst_display_rack = None
        self._vst_swap = None
        self._kept_device = None
        self._vst_page_suspended = False
        self._vst_panel_transition = None
        self._vst_pending_unmount = None
        self._vst_captures = []
        self._vst_insert_jobs = []
        self._vst_insert_queue = []
        self._vst_state_warning = None
        self._vst_fault = None
        self._vst_instrument_names = {}
        self._vst_setup = self._vst_dialog = self._vst_signature = None
        self._vst_factory = vst_session_factory
        from astra.vst_host.session import PluginStandby
        self._instrument_standby = PluginStandby() if vst_session_factory is None else None
        self._vst_feedback = True
        self._vst_midi_gain = None
        super().__init__(*args, **kwargs)
        self._vst_dispatcher = _Dispatch(self)
        self._vst_dispatcher.done.connect(self._vst_pump, Qt.ConnectionType.QueuedConnection)
        from .vst_project import read_instrument_names
        self._vst_instrument_names_load = self._vst_dispatcher.submit(read_instrument_names)
        QTimer.singleShot(0, self._prewarm_shared_endpoint)
        QTimer.singleShot(50, self._plugin_startup)
        if self._instrument_standby is not None:
            QTimer.singleShot(50, self._prime_instrument_standby)

    def _prime_instrument_standby(self):
        if not self._closed and not self._output_shutdown and self._instrument_standby is not None:
            self._instrument_standby.prime()

    def _prewarm_shared_endpoint(self):
        if (self._closed or self._output_shutdown or not getattr(self, '_native_tracks', False)
                or self._vst_factory is not None or self._kept_device is not None
                or getattr(self, '_asio_panel', None) is not None
                or getattr(self._dm(), 'awaiting_asio_confirmation', False)):
            return
        try:
            self._ensure_kept_endpoint(direct_mode=True)
        except Exception:
            # Never fail startup if hardware ASIO is currently unplugged; normal
            # project open / Play will report any real device failure.
            return

    def enter_score_transport(self, project, native_editor=None):
        """A score route claims only a currently mounted piano rack."""
        r,b = self._vst,self._vst_binding
        if (r is not None and getattr(self, '_vst_page_suspended', False) and not r.retiring
                and self._vst_matches(b) and (self._project or {}).get('path') == project):
            self._pause_shared_tracks_for_vst()
            self._vst_page_suspended = False
        if (r is None or r.retiring or not self._vst_matches(b) or not r.ready or
                self._vst_pending_transport() is not None or r.restoring_notes_rate or (self._project or {}).get('path') != project):
            if native_editor is not None:
                transport_pages.defer_score(self, native_editor)
                if r is None and (self._project or {}).get('path') == project:
                    transport_pages.builtin_score(self, native_editor, project)
            return False
        prior=getattr(self,'_score_transport',None)
        if prior is not None and prior['route'] is r:
            if native_editor is not None:
                transport_pages.resume_score(self, native_editor, prior)
            return True
        position=transport_pages.notes_for_score(self,b,r.requested_position)
        self._score_transport=dict(route=r, binding=b, position=position, loop=r.loop,
                                   prior_rate=r.requested_rate, revision=None, desired=None, bar_authority=None,
                                   score_range=None, written_bounds=None, score_markers=None,
                                   click_available=False, metronome=False, count_geometry=None, count_in=self._score_pref('score_count_in'), beat_map=None, ticks=None, bridge=None)
        r.begin_score_source()
        r.set_rate(1.)
        if native_editor is not None:
            transport_pages.resume_score(self, native_editor, self._score_transport)
        self._notify_midi()
        return True

    def leave_score_transport(self):
        scope,self._score_transport=getattr(self,'_score_transport',None),None
        if scope is None:
            return
        r,b=scope['route'],scope['binding']
        if r is self._vst and not r.retiring and getattr(r, 'score_only', False):
            if self._vst_choice and self._vst_choice[1] is r.instrument:
                self._vst_choice = None
            self._vst_leave(capture=False, reason='score_builtin_left')
            self._notify_midi()
            return
        if r is self._vst and not r.retiring:
            if not self._vst_matches(b):
                self._vst_leave(reason='stale_score_source')
                return
            head=b.owner.head
            r.leave_score_source(b.owner.freeze(head['stamp']))
            r.music_revision=head['revision']
            r._restore_notes_seek=(r.wanted,min(scope['position'],r.duration))
            r.set_loop(*(scope['loop'] or (0.,0.,False)))
            r.restore_notes_rate(scope['prior_rate'])
            self._vst_range=None
            self._notify_midi()

    def score_transport_revision(self, revision):
        scope=getattr(self,'_score_transport',None)
        if scope is not None:
            scope['desired']=None
            scope['revision']=None
            scope['bar_authority']=scope['score_range']=scope['ticks']=None
            scope['score_markers']=scope['written_bounds']=None
            scope['click_available']=False
            scope['count_geometry']=scope['beat_map']=None
            if scope['route'] is self._vst:
                scope['route'].cancel_score_seek()
                scope['route'].set_loop(0.,0.,False)
            if scope['route'] is self._vst and scope['route'].wants_play:
                scope['route'].transport('pause')
                self._notify_midi()

    def score_transport_loop(self, bridge, start, end, active):
        from math import isfinite
        scope=getattr(self,'_score_transport',None)
        if not scope or scope['route'] is not self._vst or not self._vst_matches(scope['binding']):
            return
        r=scope['route']
        authority=scope['bar_authority']
        if (not authority or r.retiring or type(active) is not bool or not isfinite(start) or not isfinite(end)
                or not 0.<=start<end<=r.duration
                or not authority._owned(str(id(bridge.process)),bridge._native_revision,r.applied)):
            return
        scope['score_range']=(start,end,active)
        scope['score_markers']=None
        r.set_loop(start,end,active)
        self._notify_midi()

    def _set_score_markers(self, bridge, scope, action):
        from fractions import Fraction
        from .vst_projection import sample_of
        ticks,bounds = scope['ticks'],scope['written_bounds']
        r=scope['route']
        if (not ticks or not bounds or scope['revision'] != bridge._native_revision
                or r.applied is None or r.applied != r.wanted or r.projection_error):
            return False
        current=scope['score_markers']
        if action=='loop' and scope['score_range'] and scope['score_range'][2]:
            left,right,_=scope['score_range']
            scope['score_range']=(left,right,False)
            r.set_loop(left,right,False)
            return True
        first,last=bounds
        raw_left,raw_right=current or bounds
        selection=getattr(bridge,'_playback_selection',None) or {}
        start,end=selection.get('tick'),selection.get('endTick')
        valid=lambda t:type(t) is int and first<=t<=last
        if action=='loop':
            if selection.get('kind')=='range' and valid(start) and valid(end) and start<end:
                raw_left,raw_right=start,end
        else:
            if r.playing:
                written=ticks.written_at(Fraction(str(r.position)))
                start=end=written[0] if written else None
            raw=start if action=='loopIn' else end
            if not valid(raw):
                return False
            if action=='loopIn':
                raw_left=raw
                if raw_left>=raw_right:
                    raw_right=last
            else:
                raw_right=raw
                if raw_right<=raw_left:
                    raw_left=first
        if not first<=raw_left<raw_right<=last:
            return False
        # Pinned NotationPlayback derives each marker from its first played
        # occurrence; these remain written markers, not linear score seconds.
        left=ticks.resolve(raw_left)
        right=r.duration if raw_right==last else ticks.resolve(raw_right)
        if left is None or right is None:
            return False
        left,right=float(left),float(right)
        if sample_of(left,r.rate)>=sample_of(right,r.rate):
            return False  # skipped/alternate ending can reverse actual playback order
        scope['score_range']=(left,right,True)
        scope['score_markers']=(raw_left,raw_right)
        r.set_loop(left,right,True)
        return True

    @staticmethod
    def _score_target(kind, value):
        from math import isfinite
        if kind=='seek':
            position=value.get('position') if isinstance(value,dict) else value
            if type(position) in (int,float) and 0.<=position<=1_000_000 and isfinite(position):
                return ('seconds',position)
        elif kind=='seekSelection':
            return ('selection',)
        elif kind=='seekTick' and isinstance(value,dict):
            tick,occ=value.get('tick'),value.get('occurrence',1)
            if type(tick) is int and 0<=tick<2**31 and type(occ) is int and 1<=occ<=12000:
                return ('tick',tick,occ)
        elif kind=='seekBeat' and isinstance(value,dict):
            measure,beat=value.get('measure'),value.get('beat')
            if type(measure) is int and type(beat) is int and 1<=measure<=12000 and 1<=beat<=64:
                return ('beat',measure,beat)
        elif kind=='rewind':
            return ('start',)
        return None

    @staticmethod
    def _score_position(target, scope, selection):
        r=scope['route']
        if target[0]=='seconds':
            return target[1] if target[1]<=r.duration else None
        if target[0]=='start':
            loop=scope['score_range']
            return loop[0] if loop and loop[2] else 0.
        if target[0]=='selection':
            if not isinstance(selection,dict):
                return None
            target=('tick',selection.get('tick'),1)
        seek_beat=target[0]=='beat'
        if seek_beat:
            beats=scope['beat_map']
            raw=beats.resolve(*target[1:]) if beats else None
            if raw is None:
                return None
            target=('tick',raw,1)  # Stock score toolbar seeks first played occurrence.
        ticks=scope['ticks']
        value=ticks.resolve(*target[1:]) if ticks else None
        if seek_beat and value is not None:
            loop=scope['score_range']
            if loop and loop[2]:
                value=max(float(value),loop[0])
                if value>=loop[1]:
                    return None  # Existing VST loop is half-open, never restart it silently.
        return float(value) if value is not None else None

    def _active_score_route(self, bridge):
        scope=getattr(self,'_score_transport',None)
        if not scope or scope['route'] is not self._vst:
            return None
        r=scope['route']
        if r.retiring or not self._vst_matches(scope['binding']):
            return None
        if scope['bridge'] is not None and scope['bridge'] is not bridge.process:
            return None
        return scope

    def score_transport_action(self, bridge, kind, position=0.):
        scope=self._active_score_route(bridge)
        if scope is None:
            return
        r=scope['route']
        if kind=='speed':
            if not isinstance(position,dict) or (self._vst_pending_transport() is not None):
                return
            try:
                r.set_rate(position.get('multiplier'),score=True)
            except ValueError:
                return
        elif kind=='flatTempo':
            # Ignore score tempo (Astra option): rebuild the score timeline at the
            # opening tempo and continue from the same written position.
            enabled=position.get('enabled') if isinstance(position,dict) else None
            if type(enabled) is not bool or enabled==self._score_flat():
                return
            self._score_flat_tempo=enabled
            self._remember_score_pref('score_flat_tempo',enabled)
            target=None
            ticks=scope['ticks']
            if ticks is not None and scope['revision'] is not None:
                from fractions import Fraction
                written=ticks.written_at(Fraction(str(r.position)))
                if written is not None:
                    target=('tick',written[0],written[1])
            playing=bool(r.wants_play)
            self.score_transport_revision(bridge._native_revision)
            self._prepare_score_transport(bridge,scope,playing,target)
        elif kind=='countIn':
            enabled=position.get('enabled') if isinstance(position,dict) else None
            if type(enabled) is not bool or not scope['count_geometry'] or scope['revision']!=bridge._native_revision:
                return
            scope['count_in']=enabled
            self._remember_score_pref('score_count_in',enabled)
        elif kind=='metronome':
            if not scope['click_available'] or scope['revision']!=bridge._native_revision or r.applied!=r.wanted:
                return
            enabled=position.get('enabled') if isinstance(position,dict) else None
            if type(enabled) is not bool:
                return
            scope['metronome']=enabled
            r.set_mix(self._vst_mix())
        elif kind in ('loop','loopIn','loopOut'):
            if not self._set_score_markers(bridge,scope,kind):
                return
        elif kind=='stop':
            if scope['desired']:
                # Keep the already requested native snapshot warm for an
                # immediate Play after Stop without reviving canceled intent.
                scope['desired'].update(play=False,target=('start',),discard=True)
            r.cancel_score_seek()
            r.transport('stop')
        elif kind=='pause':
            if scope['desired']:
                scope['desired']['play']=False
            r.transport('pause')
        elif (target:=self._score_target(kind,position)) is not None:
            pending=scope['desired']
            if pending:
                pending.update(target=target,discard=False)
            elif scope['revision'] is not None and scope['revision']==bridge._native_revision and not r.projection_error:
                selected=getattr(bridge,'_playback_selection',None)
                aim=self._score_position(target,scope,selected)
                if aim is None:
                    return
                if scope['count_in'] and r.wants_play and not r.playing:
                    r.transport('pause')
                r.seek_score_source(aim)
            else:
                self._prepare_score_transport(bridge,scope,False,target)
        else:
            return
        self._notify_midi()

    def toggle_score_transport(self, bridge):
        scope=self._active_score_route(bridge)
        if scope is None or self._vst_pending_transport() is not None:
            return
        r=scope['route']
        pending=scope['desired']
        if pending is not None:
            pending.update(play=not pending['play'],discard=False)
        elif r.wants_play:
            r.transport('pause')
        elif scope['revision'] is not None and scope['revision']==bridge._native_revision and not r.projection_error:
            transport=(r.wire.get('transport') or {}).get('state')
            loop=scope['score_range']
            start,end=(loop[0],loop[1]) if loop and loop[2] else (0.,r.duration)
            # A stopped loop starts at its in-point. A paused VST resumes its
            # rendered position, except at the same playback end as MuseScore.
            if (transport=='stopped' and loop and loop[2]) or r.position>=r.duration-0.5/r.rate:
                if r.wanted==r.applied and getattr(r,'_score_seek',None) is None:
                    r.seek_score_source(start)
            self._play_score(scope)
        else:
            self._prepare_score_transport(bridge,scope,True,None)
        self._notify_midi()

    @staticmethod
    def _play_score(scope):
        r=scope['route']
        lead=None
        if scope['count_in'] and scope['count_geometry'] and scope['ticks']:
            from .native_score_metronome import count_plan
            pending=getattr(r,'_score_seek',None)
            aim=pending[1] if pending and pending[0]==r.wanted else r.position
            lead=count_plan(scope['ticks'],scope['count_geometry'],aim,r.requested_rate,r.rate)
            if lead is None:
                r.notice='No native count-in for this score position'
        r.transport('play',count_in=lead)

    def _prepare_score_transport(self, bridge, scope, autoplay, target):
        revision, process=bridge._native_revision,bridge.process
        if revision is None or process is None:
            return
        r=scope['route']
        intent=dict(play=autoplay,target=target,discard=False)
        scope['desired']=intent
        project=scope['binding'].project['path']
        native_caps=r.wire.get('capabilities') or {}
        supports_clicks=bool(native_caps.get('score_click_gates'))
        supports_count=bool(native_caps.get('score_count_in'))
        def current():
            return (self._active_score_route(bridge) is scope and scope['desired'] is intent
                    and bridge.process is process and bridge._native_revision==revision)
        def finish(snapshot, error):
            if not current():
                return
            if intent['discard']:
                scope['desired']=None
                return
            if error:
                scope['desired']=None
                r.notice=error
                self._notify_midi()
                return
            from .vst_score_snapshot import decode_live_score, flatten_tempo
            from .native_score_bar_map import decode_bar_map,decode_playback_ticks,decode_beat_geometry,BarPlaybackAuthority
            flat=self._score_flat()
            if flat:
                snapshot=(*snapshot[:2],flatten_tempo(snapshot[2]),*snapshot[3:])
            def decode():
                score=decode_live_score(snapshot[2])
                packet=snapshot[3] if len(snapshot)>3 else None
                bars=decode_bar_map(snapshot[2],score,packet) if packet is not None else None
                ticks=decode_playback_ticks(snapshot[2],score,packet)
                if bars:
                    score=replace(score,duration=max(score.duration,bars[-1].end))
                if supports_clicks and ticks and (packet or {}).get('clickGeometry'):
                    from .native_score_metronome import project_clicks
                    try:
                        score=replace(score,clicks=project_clicks(ticks,packet['clickGeometry']))
                    except ValueError:
                        pass  # An unsupported grid must not invalidate playable MIDI.
                bounds=(packet or {}).get('writtenBounds')
                if (not isinstance(bounds,list) or len(bounds)!=2 or any(type(v) is not int for v in bounds)
                        or not 0<=bounds[0]<bounds[1]<2**31):
                    bounds=None
                count_geometry=None
                if supports_count and score.clicks:
                    rows=packet['clickGeometry']
                    if all(len(row)==9 and type(row[7]) is int and type(row[8]) is int
                           and 0<=row[8]<row[7]<=1920 for row in rows):
                        count_geometry=tuple(map(tuple,rows))
                beats=decode_beat_geometry((packet or {}).get('beatGeometry'),bounds)
                return score,bars,ticks,(packet or {}).get('selection'),bounds,count_geometry,beats
            task=self._vst_dispatcher.submit(decode)
            def apply():
                if not current():
                    return
                if not task.done():
                    QTimer.singleShot(12,apply)
                    return
                scope['desired']=None
                if intent['discard']:
                    return
                try:
                    score,bars,ticks,selected,bounds,count_geometry,beats=task.result()
                    r.offer_score_source(score)
                    scope['ticks'],scope['bridge'],scope['written_bounds']=ticks,process,bounds
                    scope['click_available']=bool(score.clicks)
                    scope['count_geometry'],scope['beat_map']=count_geometry,beats
                    scope['bar_authority']=BarPlaybackAuthority(str(id(process)),revision,r.wanted,bars) if bars else None
                    desired=intent['target']
                    aim=self._score_position(desired or ('selection',),scope,selected)
                    if desired is not None and aim is None:
                        r.notice='Score selection is not in the played timeline'
                    else:
                        r.notice=None
                    r.seek_score_source(0. if aim is None else aim)
                    if intent['play'] and (aim is not None or desired is None):
                        self._play_score(scope)
                    scope['revision']=revision
                except (OSError,ValueError,TypeError,RuntimeError,EOFError) as exc:
                    r.notice=str(exc)
                self._notify_midi()
            apply()
        try:
            bridge.request_playback_snapshot(project,finish)
        except ValueError as exc:
            scope['desired']=None
            r.notice=str(exc)

    def _vst_context(self):
        project = self._project
        doc = (project or {}).get('editor') or {}
        entry = live_owner.retained(project['path']) if doc else None
        if entry is None or doc.get('preview_path') != self._editor_uri or not self._editor_native_stamp:
            raise ValueError('没有当前的原生编辑工程。')
        if tuple(entry.head['stamp']) != tuple(self._editor_native_stamp):
            raise ValueError('编辑工程已切换；请重新打开乐器设置。')
        return _Binding(project, entry, self._editor_uri, entry.head['source_key'], entry.head['stamp'][0])

    def _vst_matches(self, b):
        p = self._project
        if b is None or b.project is not p or self._editor_uri != b.uri:
            return False
        entry = live_owner.retained(p['path'])
        return (entry is b.owner and not entry.closed and entry.head['stamp'][0] == b.native_epoch and
                entry.head['source_key'] == b.source and (p.get('editor') or {}).get('preview_path') == b.uri)

    def _open_vst_settings(self, message, *, _from_insert_queue=False):
        if self._closed or self._output_shutdown or getattr(self, '_editor_closing', False):
            return
        try:
            scope = json.loads(message)
            b = self._vst_context()
            if scope.get('project') != b.project['path'] or scope.get('generation') != self._edit_generation_number:
                return
            action = scope.get('action', 'list')
            if action in ('mount','unmount') and not _from_insert_queue and (self._vst_insert_jobs or self._vst_insert_queue or self._vst_pending_transport() is not None):
                if action == 'mount' and self._vst_insert_queue:
                    prior, request = self._vst_insert_queue[-1]
                    if (request.get('action') == 'mount' and request.get('generation') == scope['generation']
                            and prior.owner is b.owner and prior.native_epoch == b.native_epoch and self._vst_matches(prior)):
                        self._vst_insert_queue.pop()
                self._vst_insert_queue.append((b,dict(scope)))
                return
            if action.startswith('insert_'):
                self._vst_insert_action(b,scope)
            elif action == 'unmount':
                self._vst_unmount(b, _from_insert_queue=_from_insert_queue)
            elif action in ('panel', 'panel_close', 'save_default_patch'):
                if self._vst is None or not self._vst.ready:
                    raise ValueError('请先选择 MIDI 播放并等待乐器就绪。')
                if ('editor_route' in scope and scope['editor_route'] != self._vst.epoch or
                        'key' in scope and scope['key'] != self._vst.instrument.key):
                    return
                if action == 'save_default_patch':
                    if self._vst.wants_play or self._vst.playing or self._vst_swap:
                        raise ValueError('请在暂停时保存初始音色。')
                    self._vst.plugin_action(action)
                else:
                    self._vst.plugin_action('editor_close' if action == 'panel_close' else 'editor_open')
            elif action in ('mount', 'reconnect'):
                fault = self._vst_current_fault()
                attempt = saved_restore.current(self)
                key = scope.get('key')
                saved_key = ((attempt.result or {}).get('identity') or (None,None))[0] if attempt else None
                wants_saved = (action == 'reconnect' and not fault or
                               action == 'mount' and key is not None and key == saved_key and
                               attempt.phase == 'failed' and self._vst is None)
                if wants_saved:
                    if not key or not saved_restore.start(self, b, retry_key=key):
                        raise ValueError('已保存音源暂时不能重新连接；请检查插件和全局设备，或重新打开本工程。')
                    self._vst_pump()
                    return
                if action == 'reconnect' and (not fault or not self._vst_can_reconnect()):
                    raise ValueError('当前音源尚不能重新连接，请等待原音频端点释放。')
                catalog = self._vst_catalog
                if not catalog or not self._vst_matches(catalog[0]):
                    raise ValueError('乐器目录尚未就绪，请重新打开乐器菜单。')
                libraries, instruments, choices, _, error = catalog[1]
                instrument = instruments.get(scope.get('key'))
                if instrument is None or libraries is None:
                    raise ValueError(error or '所选乐器已不在目录中。')
                if action == 'reconnect' and (instrument.key, instrument.class_id.lower()) != (
                        fault['instrument'].key, fault['instrument'].class_id.lower()):
                    raise ValueError('所选乐器已变化，请重新选择音源。')
                try:
                    device = global_device_route(self._dm().current, choices, active_endpoint=self._kept_device)
                except ValueError as exc:
                    if action == 'reconnect':
                        raise
                    device = None
                    self._vst_state_warning = str(exc)
                if (self._vst and self._vst.ready and self._vst.instrument == instrument and
                        self._kept_device and self._kept_device.matches(device)):
                    return
                saved_restore.abandon(self, include_background=True)
                if self._vst_swap is not None and self._vst_swap.get('background_suspend'):
                    self._vst_cancel_swap()
                self._vst_choose(b, instrument, libraries, device, True)
            elif action == 'choose_preferred_effect':
                from astra.pipeline import save_json
                key = scope.get('key') or None
                if key is not None and not self._vst_effect_catalog.get(key,{}).get('available'):
                    raise ValueError('请选择目录中可用的效果器。')
                save_json(self._favorite_path,key)
                self._favorite_effect = key
            elif action in ('list', 'choose', 'list_effects', 'list_inventory'):
                if (action == 'list_effects' and self._vst and self._vst.ready and self._vst_matches(self._vst_binding)
                        and not getattr(self,'_favorite_effect',None) and self._vst.wire.get('isolated_inserts') and not self._vst_pending_transport()
                        and not self._vst_insert_jobs and not self._vst_insert_queue):
                    self._vst_dispatcher.submit(self._vst.session.request, 'warm_insert_worker')
                elif action in ('list', 'choose') and self._instrument_standby is not None:
                    self._instrument_standby.prime()
                pending = self._vst_setup
                if pending is not None and not self._vst_matches(pending[0]):
                    self._vst_setup = None
                    pending[1].cancel()
                if self._vst_setup is None and (not self._vst_catalog or not self._vst_matches(self._vst_catalog[0])):
                    self._vst_read_catalog(b)
            else:
                raise ValueError('未知乐器操作。')
            self._vst_pump()
        except (AttributeError, TypeError, KeyError, ValueError, OSError, RuntimeError) as exc:
            self._vst_state_warning = str(exc)
            self.logs.emit(str(exc))
            self._notify_midi()

    def _vst_read_catalog(self, binding):
        from astra.studio_bridge import PREFERENCES
        self._vst_setup = binding, self._vst_dispatcher.submit(
            self._vst_setup_values, PREFERENCES.with_name('vst-output.json'), binding.owner.project)

    def _vst_refresh_open_catalog(self):
        binding = (self._vst_setup or self._vst_catalog or (None,))[0]
        if self._vst_setup is not None:
            self._vst_setup[1].cancel()
            self._vst_setup = None
        if binding and self._vst_matches(binding):
            self._vst_read_catalog(binding)
        else:
            self._vst_catalog = None

    @staticmethod
    def _vst_setup_values(path,project):
        from .vst_project import effect_config
        from .vst_metadata import scan_catalog
        _, coverage = scan_catalog()
        return setup_values(path,project),effect_config(path),coverage

    def _vst_set_feedback(self, enabled):
        self._vst_feedback = bool(enabled)
        if not enabled and self._vst is not None:
            self._vst.clear()
        self._vst_pump()

    def _vst_note_feedback(self, message):
        r = self._vst_active()
        if r is None or self._closed or len(message) > 1024 or not self._vst_matches(self._vst_binding):
            return
        try:
            value = json.loads(message)
        except ValueError:
            return
        if isinstance(value, dict) and (self._vst_feedback or value.get('op') in ('off', 'clear')):
            r.audition(value)
            self._vst_pump()

    def _vst_active(self):
        return None if getattr(self, '_vst_page_suspended', False) else self._vst

    def _output_shares_kept_endpoint(self):
        h = getattr(self, '_output', None)
        ep = getattr(self, '_kept_device', None)
        return bool(ep is not None and not ep.closing and h is not None and h.owner is not None
                    and not h.retiring and not h.closing
                    and getattr(h.owner, '_kept_endpoint', None) is ep)

    def _ensure_kept_endpoint(self, device=None, libraries=None, *, direct_mode=None):
        from .kept_endpoint import KeptEndpoint
        from .asio_policy import resolve_asio_device_route
        from .vst_project import application_config
        from astra.studio_bridge import PREFERENCES
        if self._kept_device is not None and self._kept_device.handle and not self._kept_device.closing:
            if device is None or self._kept_device.matches(device):
                if direct_mode is not None:
                    self._kept_device.set_direct_mode(bool(direct_mode))
                return self._kept_device
            raise RuntimeError('上一音频端点尚未释放。')
        if self._kept_device is not None and self._kept_device.closing:
            raise RuntimeError('上一音频端点正在关闭，请稍候。')
        if libraries is None:
            libraries, _ = application_config(PREFERENCES.with_name('vst-output.json'))
        if device is None:
            manager = self._dm()
            device = getattr(manager, 'manual_asio_route', None) or resolve_asio_device_route(manager.current, active_endpoint=None)
        self._kept_device = KeptEndpoint(libraries, device)
        if direct_mode is not None:
            self._kept_device.set_direct_mode(bool(direct_mode))
        return self._kept_device

    def _pause_shared_tracks_for_vst(self):
        if self._output_shares_kept_endpoint():
            h = self._output
            h.sync_web_intent()
            if h.intent.playing:
                h.change(playing=False)
                self._output_desired = h.intent
        if self._kept_device is not None:
            self._kept_device.set_direct_mode(False)

    def _vst_suspend_for_tracks(self):
        if getattr(self, '_score_transport', None):
            self.leave_score_transport()
        r = self._vst
        if r is not None:
            if r.wants_play:
                r.transport('pause')
            r.clear()
        self._vst_page_suspended = True
        if self._kept_device is not None:
            self._kept_device.set_direct_mode(True)

    def _vst_new_route(self, choice):
        binding, instrument, libraries, device, restore = choice
        factory = dict(factory=self._vst_factory) if self._vst_factory is not None else {}
        from .vst_project import effect_config
        from astra.studio_bridge import PREFERENCES
        # Use the same validated snapshot that supplied this menu selection.
        # Direct callers without a menu still get the configured FX inventory.
        effects = (self._vst_effect_catalog if self._vst_catalog and self._vst_matches(self._vst_catalog[0])
                   else effect_config(PREFERENCES.with_name('vst-output.json')))
        from .vst_project import BUILTIN_KEY
        score_only = instrument.key == BUILTIN_KEY
        route = VstRoute(binding.owner.project, instrument, libraries,
                         dict(device or {'sample_rate':48000,'buffer_frames':256}, **self._vst_mix()), self._vst_dispatcher.submit,
                         restore=restore and not score_only,
                         instrument_state=None if score_only else binding.owner.instrument_state(), effects=effects, **factory)
        route.capture_owner, route.capture_bound = (_ScoreOnlyOwner(binding.owner) if score_only else binding.owner), False
        route.score_only = score_only
        route.instrument_standby = self._instrument_standby
        if device and self._vst_factory is None:
            route.prepare_device = self._vst_prepare_device
            route.observe_capacity = self._vst_observe_capacity
        return route

    def _vst_observe_capacity(self, route):
        if getattr(self, '_settings_asio_handoff', None) is not None:
            return False
        endpoint = getattr(self, '_kept_device', None)
        # The retiring owner/tick must prove release before opening a new
        # driver. Treat that normal handoff as pending, not a plugin error.
        if endpoint is not None and endpoint.closing:
            return False
        endpoint = self._ensure_kept_endpoint(device=route.device, libraries=route.libraries)
        manager = getattr(self, '_devices_mgr', None)
        requested = manager.manual_asio_route if getattr(manager, 'state', None) == 'switching' else None
        try:
            actual = endpoint.poll()
        except (OSError, RuntimeError, ValueError) as exc:
            if requested is not None and endpoint.matches(requested):
                manager.failed(exc)
            raise
        if actual is None:
            return False
        if requested is not None:
            # With only Notes or Score active, no Tracks owner reports the
            # switch. Confirm it from the same verified physical ASIO callback.
            confirmed = endpoint.reuse_open_asio(requested, require_requested_frames=False)
            if confirmed is not None:
                manager.confirm_kept_switch(manager.current, confirmed)
        route.device['device_id'] = endpoint.device['device_id']
        route.device['native_selector'] = dict(endpoint.device['native_selector'])
        route.instrument_block_proof = ('kept', endpoint.name, actual['frames'])
        return True

    def _vst_prepare_device(self, route):
        return self._vst_observe_capacity(route) and self._kept_device.lease(route)

    def _vst_choose(self, binding, instrument, libraries, device, restore, *, staged_attachment=None, saved_reopen=None, background_suspend=False):
        manager=self._dm()
        if getattr(manager,'awaiting_asio_confirmation',False):
            raise ValueError(manager.message)
        if self._closed or self._output_shutdown or not self._vst_matches(binding):
            raise ValueError('当前工程已切换。')
        if getattr(self, '_asio_panel', None) is not None or getattr(self,'_vst_panel_transition',None):
            raise ValueError('请先关闭驱动控制面板。')
        if self._vst_swap is not None:
            raise ValueError('乐器正在切换，请稍候。')
        choice = (binding, instrument, libraries, device, restore)
        from .kept_handover import attachment_marker
        attachment_at_start = attachment_marker(binding.owner)
        candidate = self._vst_new_route(choice)
        if staged_attachment is not None:
            candidate.instrument_state = staged_attachment
        if saved_reopen is not None:
            candidate.saved_reopen = True
            candidate.saved_reopen_rack = saved_reopen.result['rack']
            candidate.saved_reopen_slots = saved_restore.slot_signature(candidate.saved_reopen_rack['slots'], persisted=True)
            candidate.instrument_state = saved_reopen.attachment
            candidate.effect_catalog = saved_reopen.result['effects']
        from .instrument_preflight import preflight_ticket
        ticket=preflight_ticket(self._vst,device)
        endpoint = getattr(self, '_kept_device', None)
        known_proof = ticket[2]
        if (known_proof is None and endpoint is not None and endpoint.info is not None
                and device is not None and endpoint.matches(device)):
            known_proof = ('kept', endpoint.name, endpoint.info['frames'])
        candidate.instrument_block_proof=known_proof
        # The kept producer slices callbacks, so opening the driver and creating
        # the first instrument can overlap. Keep the measured size for rejection recovery.
        first_probe=(known_proof is None and getattr(candidate,'observe_capacity',None) and self._vst is None
                     and self._vst_admit())
        previous, self._previous_instrument = self._previous_instrument, None
        if previous:
            owner, prior, libs, prev_endpoint, expires, session = previous
            if (restore and staged_attachment is None and saved_reopen is None and owner.owner is binding.owner
                    and self._vst_matches(owner) and prior == instrument and libs == libraries
                    and ticket[2] and prev_endpoint is self._kept_device and prev_endpoint.matches(device)
                    and perf_counter() < expires and session.process.poll() is None):
                candidate.session = session
                candidate.reused_instrument = True
            else:
                self._vst_dispatcher.submit(session.close)
        if first_probe:
            candidate.first_driver_frames = Future()
        if candidate.session is None:
            candidate._post('load')
        if device is not None:self._vst_state_warning = None
        fault = self._vst_current_fault()
        notes = None if background_suspend else self._notes_checkpoint(binding)
        self._vst_swap = dict(phase='probe' if first_probe else 'preflight', choice=choice, candidate=candidate, preflight_ticket=ticket,
                              attachment_at_start=attachment_at_start,
                              first_probe=bool(first_probe),
                              staged_attachment=staged_attachment, saved_reopen=saved_reopen,
                              background_suspend=bool(background_suspend),
                              old=self._vst, old_choice=None if fault and self._vst is None else self._vst_choice,
                              position=0. if background_suspend else (notes[0] if notes is not None else self._vst.position if self._vst else fault['position'] if fault else 0.),
                              playing=False if background_suspend else (self._vst.wants_play if self._vst else False), explicit=set())
        if notes is not None:
            self._vst_swap['loop'] = notes[1]

    def _notes_transport_binding(self):
        uri = getattr(self, '_editor_uri', None)
        if (not uri or (getattr(self, '_midi_file', None) or {}).get('path') != uri
                or getattr(getattr(self, '_vst', None), '_score_source', False)):
            return None
        swap = getattr(self, '_vst_swap', None)
        if swap and (getattr(swap.get('candidate'), '_score_source', False)
                     or getattr(swap.get('saved_reopen'), 'score_intent', None) is not None):
            return None
        try:
            return self._vst_context()
        except (KeyError, ValueError, RuntimeError):
            return None

    def _notes_session(self, binding):
        from .notes_transport import session
        return session(self, binding)

    def _notes_checkpoint(self, binding):
        from .notes_transport import checkpoint
        return checkpoint(self, binding)

    def _notes_warming_silent(self, swap, binding):
        from .notes_transport import warming_silent
        return warming_silent(self, swap, binding)

    def _vst_pending_transport(self):
        swap = self._vst_swap
        if swap is not None and swap.get('background_suspend'):
            swap = None
        return swap or getattr(self,'_vst_panel_transition',None)

    def _vst_open_asio_panel(self, driver):
        from . import asio_panel_bridge
        if self._vst_swap is not None and self._vst_swap.get('background_suspend'):
            saved_restore.abandon(self, include_background=True)
            self._vst_cancel_swap()
        if self._vst_swap is not None or getattr(self,'_vst_panel_transition',None):
            return dict(ok=False,error='正在切换乐器或音频设备，请稍候。')
        if self._vst is None:
            if self._vst_retiring:return dict(ok=False,error='正在释放音频设备，请稍候。')
            return asio_panel_bridge.open_panel(self,driver)
        if self._output_shutdown or asio_panel_bridge.busy(self):
            return dict(ok=False,error='音频设备正在关闭或控制面板已打开。')
        r=self._vst
        if not r.ready:return dict(ok=False,error='乐器尚未就绪，请稍候。')
        self._vst_panel_transition=dict(stage='capture',driver=driver,old=r,
            choice=self._vst_choice,position=r.position,playing=r.wants_play,
            pcm=r.pcm_source,loop=r.loop,explicit=set(),resume=True)
        try:
            r.capture_failed=False
            self._vst_capture(r.capture_owner,r,kind='replacement')
        except (ValueError,OSError,RuntimeError) as exc:
            self._vst_panel_transition=None
            return dict(ok=False,error=str(exc))
        self._asio_panel_status=dict(phase='releasing',driver=driver,message='正在暂退设备；工程乐器保持挂载。')
        return dict(ok=True,pending=True,control_panel=dict(self._asio_panel_status))

    def _vst_panel_tick(self):
        from . import asio_panel_bridge
        pending=getattr(self,'_vst_panel_transition',None)
        if pending is None:return
        choice=pending['choice']
        if self._closed or not choice or not self._vst_matches(choice[0]):pending['resume']=False
        if pending['stage']=='capture':
            old=pending['old']
            if any(r is old for _,r,_,_,_ in self._vst_captures):return
            if old.capture_failed:
                self._asio_panel_status=dict(phase='error',message=old.notice or '音色捕获失败，未打开驱动面板。')
                self._vst_panel_transition=None
                return
            if old is not self._vst:
                self._asio_panel_status=dict(phase='error',message='乐器状态已改变，控制面板未打开。')
                self._vst_panel_transition=None
                return
            if 'position' not in pending['explicit']:pending['position']=old.position
            if 'playing' not in pending['explicit']:pending['playing']=old.wants_play
            if self._output is not None:
                self._output.close();self._output_retiring.append(self._output);self._output=None
            self._vst_leave(capture=False, reason='asio_panel')
            pending['stage']='release'
        if pending['stage']=='release':
            if self._vst_retiring or self._output_retiring:return
            if not pending['resume']:
                self._asio_panel_status=dict(phase='error',message='乐器已切换，控制面板未打开。')
                self._vst_panel_transition=None
                return
            result=asio_panel_bridge.open_panel(self,pending['driver'])
            pending['stage']='panel'
            if not result.get('ok'):
                self._vst_state_warning=result.get('error') or '控制面板未打开。'
                self._asio_panel_status=dict(phase='error',message=self._vst_state_warning)
                self.logs.emit(self._vst_state_warning)
        if pending['stage']=='panel' and asio_panel_bridge.busy(self):
            self._asio_panel_status['message']='关闭面板后返回乐器，并保留最新播放意图。'
        if pending['stage']=='panel' and not asio_panel_bridge.busy(self):
            self._vst_panel_transition=None
            self._deferred_delta=None
            self._device_resume_position=None
            if self._asio_panel_status.get('phase')!='error':
                self._asio_panel_status['message']='驱动面板已关闭，乐器挂载与最新播放意图已保留。'
            if pending['resume'] and not self._output_shutdown and self._vst_matches(choice[0]):
                try:
                    from .vst_project import endpoint_choices
                    from . import device_catalog
                    device=global_device_route(self._dm().current,endpoint_choices(device_catalog.catalog()),active_endpoint=self._kept_device)
                    self._vst_choose(choice[0],choice[1],choice[2],device,True)
                    self._vst_swap_intent(dict(position=pending['position'],playing=pending['playing']))
                    self._vst_swap['pcm']=pending['pcm']
                    self._vst_swap['explicit'].add('pcm')
                    if pending['loop'] is not None:self._vst_swap_intent(dict(loop=pending['loop']))
                except (ValueError,OSError,RuntimeError) as exc:
                    self._vst_state_warning=str(exc);self.logs.emit(str(exc))

    def _vst_swap_intent(self, delta):
        swap = self._vst_pending_transport()
        if swap is None:return False
        from math import isfinite
        if 'playing' in delta and type(delta['playing']) is not bool:
            raise ValueError('无效的播放状态。')
        if 'position' in delta and (type(delta['position']) not in (int,float) or
                not isfinite(delta['position']) or delta['position'] < 0):
            raise ValueError('无效的播放位置。')
        values={k:delta[k] for k in ('position','playing','loop','rate') if k in delta}
        if delta.get('audition') in ('joint','midi'):
            values['pcm']=self._vst_pcm_source() if delta['audition']=='joint' else None
        if swap.get('phase')=='kept' and values:
            from .kept_handover import disarm
            if disarm(self,swap):
                return False
            swap['transport_revision']=swap.get('transport_revision',0)+1
        swap.update(values)
        swap['explicit'].update(values)
        return True

    def _select_midi(self, value, *, resume=None, prepared=None):
        if not (isinstance(value,str) and value.startswith('astra-editor:')):
            self._notes_transport_session = None
            saved_restore.abandon(self)
            self._vst_cancel_swap()
        return super()._select_midi(value,resume=resume,prepared=prepared)

    def _vst_cancel_swap(self):
        panel=getattr(self,'_vst_panel_transition',None)
        if panel is not None:panel['resume']=False
        swap=self._vst_swap
        if swap and swap.get('returning_to_source'):
            from .kept_handover import _end_return, _CONFLICT
            _end_return(self, swap, _CONFLICT)
            return
        if swap and swap.get('phase')=='kept':
            from .kept_handover import disarm
            if disarm(self,swap):return
        self._vst_swap=None
        if swap is not None:
            candidate=swap['candidate']
            if candidate is not self._vst:
                frames = getattr(candidate, 'first_driver_frames', None)
                if frames is not None:
                    frames.cancel()
                if swap.get('first_probe') and self._kept_device and not self._output_shares_kept_endpoint():
                    candidate.departing_endpoint=self._kept_device
                candidate.retire()
                if candidate not in self._vst_retiring:self._vst_retiring.append(candidate)

    def _vst_drop_stale_preflight(self,swap):
        from .instrument_preflight import ticket_current
        ticket=swap.get('preflight_ticket')
        if ticket is None or ticket_current(ticket,swap['old'],swap['choice'][3]):return False
        self._vst_state_warning='输出设备或缓冲已改变，已取消切换。'
        candidate=swap['candidate'];candidate.retire();self._vst_retiring.append(candidate)
        self._vst_swap=None
        return True

    def _vst_swap_tick(self):
        swap = self._vst_swap
        if swap is None:
            return
        candidate, binding = swap['candidate'], swap['choice'][0]
        if self._closed or not self._vst_matches(binding):
            self._vst_cancel_swap()
            return
        saved_reopen = swap.get('saved_reopen')
        if saved_reopen is not None:
            from .kept_handover import attachment_marker
            manager = self._dm()
            warning = None
            if attachment_marker(binding.owner) != saved_reopen.marker:
                warning = '准备期间工程音色已更改，已保留较新的保存状态。'
            elif (getattr(manager,'awaiting_asio_confirmation',False) or
                    saved_restore.device_settings(manager) != saved_reopen.device):
                warning = '准备期间全局音频设置已改变，未连接旧设备。'
            if warning:
                self._vst_state_warning = warning
                if self._vst is candidate:self._vst_leave(capture=False)
                self._vst_cancel_swap()
                return
        phase = swap['phase']
        if (phase in ('preflight','kept') and not swap.get('armed') and
                getattr(candidate,'reused_instrument',False) and candidate.session and
                candidate.session.process.poll() is not None and swap['old'] is self._vst and
                self._kept_device and self._kept_device.matches(swap['choice'][3])):
            # An idle worker can exit between admission and its first command.
            # The live instrument is still authoritative. Retry once with a new
            # process; the replacement does not carry the reused flag.
            candidate.retire()
            self._vst_retiring.append(candidate)
            candidate = self._vst_new_route(swap['choice'])
            candidate.instrument_block_proof = swap['preflight_ticket'][2]
            swap.update(candidate=candidate, phase='preflight', synchronized=None)
            candidate._post('load')
            return
        if phase=='probe':
            try:
                candidate._finish()
                if candidate.error:
                    raise RuntimeError(candidate.error)
                # The shared ASIO endpoint owns opening and failure, including fallback.
                if not candidate.observe_capacity(candidate):return
                candidate.first_driver_frames.set_result(candidate.instrument_block_proof[2])
                swap['phase']='preflight'
            except (ValueError,RuntimeError,OSError) as exc:
                self._vst_state_warning=str(exc)
                self._vst_cancel_swap()
            return
        if phase == 'preflight':
            candidate.pump(False)
            if candidate.error:
                self._vst_state_warning = candidate.error
                self._vst_cancel_swap()
                return
            if candidate.session is None or candidate.flight is not None:
                return
            old = swap['old']
            if old is not self._vst:
                candidate.retire();self._vst_retiring.append(candidate)
                self._vst_swap = None
                return
            endpoint = getattr(self, '_kept_device', None)
            if old and endpoint and endpoint.matches(swap['choice'][3]) and old.ready:
                if old.wire.get('isolated_inserts') and swap.get('staged_attachment') is None:
                    candidate.live_insert_source = old.session
                swap['phase'] = 'kept'
                swap['armed'] = False
                swap['deadline'] = candidate.now()+60
                return
            if swap.get('staged_attachment') is not None:
                self._vst_state_warning='准备期间音频设备已切换，原效果链已保留。'
                self._vst_cancel_swap()
                return
            if old is not None and old.ready and old.capture_bound:
                if self._vst_drop_stale_preflight(swap):return
                old.capture_failed = False
                self._vst_capture(old.capture_owner if getattr(old, 'score_only', False) else binding.owner, old,
                                  kind='replacement')
                swap['phase'] = 'capture'
            else:
                swap['phase'] = 'release'
        if swap['phase'] == 'capture':
            old = swap['old']
            if old is not self._vst:
                candidate.retire();self._vst_retiring.append(candidate)
                self._vst_swap = None
                return
            if any(r is old for _, r, _, _, _ in self._vst_captures):
                return
            if old.capture_failed:
                candidate.retire();self._vst_retiring.append(candidate)
                self._vst_swap = None
                return
            swap['phase'] = 'release'
        if swap['phase'] == 'kept':
            from .kept_handover import pump
            pump(self, swap)
            return
        if swap['phase'] == 'release':
            if any(b.owner is binding.owner and isinstance(r,str) for b,r,_,_ in self._vst_insert_jobs):
                return
            old = swap['old']
            if old is self._vst and self._vst_drop_stale_preflight(swap):return
            if old is self._vst and old is not None:
                if 'position' not in swap['explicit']:
                    notes = self._notes_checkpoint(binding)
                    swap['position'] = notes[0] if notes is not None else old.position
                    if notes is not None and 'loop' not in swap['explicit']:
                        swap['loop'] = notes[1]
                if 'playing' not in swap['explicit']:swap['playing'] = old.wants_play
                if 'pcm' not in swap['explicit']:swap['pcm'] = old.pcm_source
                self._vst_leave(capture=False)
            if self._vst_retiring:
                return
            if self._mem_goal is not None:
                notes = self._notes_checkpoint(binding)
                if notes is not None and (self._notes_warming_silent(swap, binding)
                                          or 'position' not in swap['explicit']):
                    swap['position'] = notes[0]
                    if 'loop' not in swap['explicit']:swap['loop'] = notes[1]
            # Same-plugin device changes restore the fresh pre-handoff capture.
            if swap['staged_attachment'] is None and saved_reopen is None:
                candidate.instrument_state = binding.owner.instrument_state()
            if candidate.flight is None and not candidate.rack_restored:
                candidate._post('restore_rack')
            if swap['choice'][3] is None:
                swap['snapshot'] = None
                swap['phase'] = 'mounted_only'
                return
            swap['phase'] = 'opening'
            background_suspend = bool(swap.get('background_suspend'))
            self._vst_enter(swap['choice'], swap['position'], prepared=candidate, suspended=background_suspend)
            if not background_suspend:
                if 'pcm' in swap:candidate.set_pcm(swap['pcm'])
                if 'loop' in swap:candidate.set_loop(*swap['loop'])
                if swap['playing'] and (saved_reopen is None or saved_reopen.play_requested):
                    candidate.transport('play')
        elif swap['phase'] == 'mounted_only':
            candidate.pump(False)
            if candidate.error:
                self._vst_state_warning=candidate.error
                candidate.retire();self._vst_retiring.append(candidate)
                swap['phase']='rollback'
                return
            if swap['snapshot'] is None:
                if candidate.flight is not None:return
                swap['snapshot']=Future()
                candidate._post('capture_state',marker=swap['snapshot'])
                return
            if not swap['snapshot'].done():return
            try:
                snapshot=swap['snapshot'].result()
                binding.owner.bind_instrument_route(candidate.epoch,snapshot['generation'],changed=True)
                binding.owner.update_instrument_state(snapshot['capture'],route_token=candidate.epoch,
                                                      generation=snapshot['generation'])
                self._editor_native_stamp=tuple(binding.owner.head['stamp'])
                self._vst_choice=swap['choice']
                self._vst_fault = None
                self._vst_remember_name(candidate.instrument)
                self._edit_emit_save_status()
                self._vst_swap=None
            except (ValueError,OSError,RuntimeError) as exc:
                self._vst_state_warning=str(exc)
                swap['phase']='rollback'
            candidate.retire();self._vst_retiring.append(candidate)
        elif swap['phase'] == 'opening':
            if candidate.error:
                self._vst_state_warning = candidate.error
                self._vst_leave(capture=False)
                swap['phase'] = 'rollback'
            elif candidate.ready:
                self._vst_choice = swap['choice']
                self._vst_fault = None
                self._vst_remember_name(candidate.instrument)
                self._vst_swap = None
        elif swap['phase'] == 'rollback' and not self._vst_retiring:
            old_choice = swap['old_choice']
            self._vst_swap = None  # One restore attempt, never an automatic retry loop.
            if old_choice and self._vst_matches(old_choice[0]):
                self._vst_choice = old_choice
                self._vst_enter(old_choice, swap['position'])
                if 'pcm' in swap:self._vst.set_pcm(swap['pcm'])
                if 'loop' in swap:self._vst.set_loop(*swap['loop'])
                # A rejected Notes renderer cannot re-authorize sound after
                # the old renderer was already stopped and released.
                if swap['playing'] and self._notes_transport_binding() is None:
                    self._vst.transport('play')

    def _vst_enter(self, choice, position, *, prepared=None, suspended=False):
        binding, instrument, libraries, device, restore = choice
        notes_active = (not suspended) and self._notes_transport_binding() is not None and self._vst_matches(binding)
        if device is None:
            raise ValueError('乐器已挂载；请在全局音频设置中选择有效输出设备后播放。')
        swap = self._vst_swap
        if suspended:
            # Silent background pre-warm on the shared ASIO KeptEndpoint:
            # never pause or displace Tracks audio, never auto-play, never alter Tracks UI state.
            self._vst_page_suspended = True
            self._vst_binding = binding
            self._vst = prepared if prepared is not None else self._vst_new_route(choice)
            self._vst.set_rate(getattr(self, '_audition_rate', 1.))
            if getattr(self._vst, 'saved_reopen', None) is not None:
                self._vst.saved_reopen_entered = True
            self._vst_range = None
            self._audition_range = None
            return
        # _FileStudioBridge._clear_midi dynamically suspends MIDI before
        # releasing the old adapter. That internal call must not cancel the
        # very saved candidate now taking over.
        internal = swap if (swap and swap.get('saved_reopen') and swap['candidate'] is prepared) else None
        previous = getattr(self, '_vst_internal_midi_clear', None)
        self._vst_internal_midi_clear = internal
        try:
            self._clear_midi()
        finally:
            self._vst_internal_midi_clear = previous
        prior = getattr(self, '_workspace_midi', None)
        if prior is not None:
            prior.close()
            self._workspace_midi = None
        self._deferred_delta = self._pending_audio = self._device_resume_position = None
        if self._output_shares_kept_endpoint():
            self._pause_shared_tracks_for_vst()
        else:
            self._retire_output_for_midi()
            if self._kept_device is not None:
                self._kept_device.set_direct_mode(False)
        self._vst_page_suspended = False
        self._output_desired = replace(self._output_desired, audition='audio', playing=False)
        self._audition_together = False
        self._edit_preview_invalidate()
        self._midi_file = dict(path=binding.uri, name='编辑版本', notes=[])
        self._vst_binding = binding
        self._vst = prepared if prepared is not None else self._vst_new_route(choice)
        self._vst.set_rate(getattr(self, '_audition_rate', 1.))
        if getattr(self._vst, 'saved_reopen', None) is not None:
            self._vst.saved_reopen_entered = True
        self._vst_range = None
        self._audition_range = None
        if notes_active and swap is not None and 'loop' in swap:
            # Do not let first publication of an unchanged document range
            # replace the Notes loop transferred from the previous renderer.
            current = binding.owner.head.get('range')
            if current is not None:
                self._vst_range = (current['start'], current['end'], current['loop'])
            a, b, enabled = swap['loop']
            self._audition_range = dict(start=a, end=b, loop=enabled)
        self._playback_status_ack = None
        if position:
            if notes_active:
                self._vst.transport('seek', position, track_notes_seek=True)
            else:
                self._vst.transport('seek', position)

    def _nav_parking_discard(self):
        slot=getattr(self,'_nav_parking',None)
        if not slot:return
        self._nav_parking=None
        if slot.get('session') is not None:
            self._vst_dispatcher.submit(slot['session'].close)

    def _nav_parking_tick(self):
        slot=getattr(self,'_nav_parking',None)
        if not slot:return
        binding=slot['choice'][0]
        if self._closed or not self._vst_matches(binding):
            self._nav_parking_discard()
            return
        if slot['settings']!=saved_restore.device_settings(self._dm()):
            if self._vst_retiring:return
            self._nav_parking_discard()
            if slot['returning']:self._vst_maybe_restore_saved(binding.uri)
            return
        if not slot.get('returning') or not slot.get('session') or self._vst_retiring:
            return
        from .navigation_bridge_resume import ResumeReceipt,resume_navigation
        _,instrument,libraries,device,_=slot['choice']
        ack=slot['receipt']
        receipt=ResumeReceipt(binding.owner,binding.uri,instrument,libraries,slot['settings'],
                             ack['generation'],ack['sample_rate'],slot['frames'],slot['session'])
        if self._vst_choice is not slot['choice'] or ack['generation']!=slot.get('capture_generation') or binding.owner.instrument_state()!=slot['attachment']:
            self._nav_parking_discard()
            self._vst_maybe_restore_saved(binding.uri)
            return
        result=resume_navigation(self,receipt,slot['choice'],position=slot['position'],
                             rack_generation=ack['generation'],device_signature=slot['settings'],
                             release_confirmed=True,expected_generation=1,generation=1)
        if result=='resume':
            self._nav_parking=None
        elif result=='miss':
            self._nav_parking_discard()
            self._vst_maybe_restore_saved(binding.uri)

    def _retire_output_for_midi(self):
        if self._output_shares_kept_endpoint():
            self._pause_shared_tracks_for_vst()
            return
        return super()._retire_output_for_midi()

    def _vst_leave(self, *, capture=True, reason=None):
        self._vst_page_suspended = False
        self._vst_release_previous()
        for departing in self._vst_retiring:
            departing.standby_binding = None
        if capture:
            for old in self._vst_retiring:
                if getattr(old,'capture_failed',False) and old.ready:
                    self._vst_capture(old.capture_owner, old, kind='departure')
        r, self._vst = self._vst, None
        self._vst_binding = None
        if r is not None:
            if reason and (r.playing or r.wants_play):
                self._vst_write_runtime(r, 'playing_route_retired', reason)
            keep_shared = (self._output_shares_kept_endpoint()
                           and not self._output_shutdown
                           and reason not in ('application_closing', 'device_switch', 'asio_panel'))
            if getattr(self,'_kept_device',None) and getattr(r,'prepare_device',None) and not keep_shared:
                r.departing_endpoint=self._kept_device
            if not r.released or getattr(getattr(r,'departing_endpoint',None),'handle',None):
                self._vst_retiring.append(r)  # Retain ownership even if capture admission fails.
            try:
                if capture and r.ready and getattr(r, 'capture_bound', False) and not r.error:
                    self._vst_capture(r.capture_owner, r, kind='departure')
                else:
                    r.retire()
            except (OSError, ValueError, RuntimeError) as exc:
                r.capture_failed = True
                r.notice = str(exc)
                self._vst_state_warning = str(exc)
                self.logs.emit(str(exc))
            self._playback_status_ack = None
            if not self._closed:
                self._player_tick()
                self._notify_midi()

    def _vst_unmount(self, binding, *, _from_insert_queue=False):
        saved_restore.abandon(self, include_background=True)
        if self._vst_swap is not None and self._vst_swap.get('background_suspend'):
            self._vst_cancel_swap()
        if not self._vst_matches(binding):
            raise ValueError('当前工程已切换。')
        if self._vst_swap is not None:
            raise ValueError('乐器正在切换，请稍候。')
        if not _from_insert_queue and (self._vst_insert_jobs or self._vst_insert_queue):
            self._vst_insert_queue.append((binding,dict(project=binding.project['path'],generation=self._edit_generation_number,action='unmount')))
            return
        notes = self._notes_checkpoint(binding)
        if notes is not None:
            self._notes_session(binding).parked = notes
        self._vst_choice = None
        self._vst_fault = None
        self._vst_pending_unmount = binding
        self._vst_leave(reason='explicit_unmount')
        self._vst_pump()

    def _vst_finish_unmount(self):
        pending = self._vst_pending_unmount
        if self._vst_retiring or pending is None:
            return
        self._vst_pending_unmount = None
        if not self._vst_matches(pending):
            return
        from uuid import uuid4
        token='unmount:'+uuid4().hex
        pending.owner.bind_instrument_route(token,0)
        from astra.vst_host.rack_state import from_attachment,attachment
        rack=from_attachment(pending.owner.instrument_state())
        rack['instrument']=None
        value=attachment(rack) if rack['slots'] else None
        pending.owner.update_instrument_state(value,route_token=token,generation=0)
        self._editor_native_stamp=tuple(pending.owner.head['stamp'])
        self._edit_emit_save_status()
        notes = self._notes_checkpoint(pending)
        if notes is not None:
            try:
                self._notes_resume_silent(pending, notes)
            except (KeyError, ValueError, RuntimeError) as exc:
                # Keep the exact retained locator for the next explicit
                # Play if document reconstruction is briefly unavailable.
                self.logs.emit(str(exc))

    def _vst_insert_action(self,binding,scope):
        from astra.vst_host.rack_state import from_attachment
        action=scope['action']
        if self._vst_insert_jobs or self._vst_retiring or self._vst_pending_unmount is not None or (self._vst_pending_transport() is not None):
            self._vst_insert_queue.append((binding,dict(scope)))
            return
        route=self._vst
        if action=='insert_remove' and getattr(route,'fx_after_snapshot',None):
            route.fx_after_snapshot[:] = [item for item in route.fx_after_snapshot
                                          if item[1].get('slot_id') != scope['slot_id']]
        if action=='insert_editor':
            if route is None or not route.ready:raise ValueError('请先启用乐器播放后打开效果器面板。')
            sid=scope['slot_id']
            if (not self._vst_matches(self._vst_binding) or
                    'editor_route' in scope and scope['editor_route'] != route.epoch or route.insert_editor_pending(sid)):
                return
            args=dict(slot_id=sid,open=scope.get('enabled', True))
            for k in ('key','rack_revision'):
                if k in scope:args[k]=scope[k]
            route.queue_insert_editor(args)
            return
        if route is None:
            from uuid import uuid4
            token='inserts:'+uuid4().hex
            binding.owner.bind_instrument_route(token,0)
            ticket=binding.owner.begin_instrument_capture(token,0)
            try:
                saved=binding.owner.instrument_state()
                future=self._vst_dispatcher.submit(self._vst_dormant_insert,saved,scope)
            except BaseException:
                binding.owner.end_instrument_capture(ticket)
                raise
            future.effect_key=scope.get('key')
            self._vst_insert_jobs.append((binding,token,ticket,future))
            return
        if not route.ready:
            raise ValueError('乐器正在准备，请稍后重试。')
        if action not in ('insert_add','insert_remove','insert_bypass','insert_move'):
            raise ValueError('Unknown insert action')
        if action == 'insert_add' and not route.wire.get('isolated_inserts'):
            self._vst_prepare_insert(binding, route, scope)
            return
        args={k:scope[k] for k in ('action','slot_id','key','enabled','to_index') if k in scope}
        generation=route.wire.get('state_generation',0)
        ticket=binding.owner.begin_instrument_capture(route.epoch,generation)
        try:future=route.insert_action(args)
        except BaseException:
            binding.owner.end_instrument_capture(ticket)
            raise
        future.effect_key=scope.get('key')
        self._vst_insert_jobs.append((binding,route,ticket,future))

    def _vst_prepare_insert(self, binding, old, scope):
        from functools import partial
        from .staged_insert import StagedInsert, checkpoint_attachment, slot_image
        generation = old.wire.get('state_generation', 0)
        checkpoint = getattr(old, 'fx_checkpoint', None)
        if (not getattr(self,'_kept_device',None) or self._vst_choice is None or
                not self._vst_matches(self._vst_choice[0]) or
                not self._kept_device.matches(self._vst_choice[3])):
            raise ValueError('音频端点正在切换，请稍后再次添加效果器。')
        if scope.get('key') not in old.effect_catalog:
            raise ValueError('所选效果器未安装或尚未完成扫描。')
        if checkpoint is None or checkpoint['generation'] != generation or getattr(old,'capture_failed',False):
            if not hasattr(old, 'fx_after_snapshot'):
                old.fx_after_snapshot = []
            # A repeated click for the same slot supersedes the previous draft.
            old.fx_after_snapshot[:] = [item for item in old.fx_after_snapshot
                                        if item[1].get('slot_id') != scope['slot_id']]
            old.fx_after_snapshot.append((binding, dict(scope)))
            self._vst_state_warning = '音色参数已更改，暂停播放时将自动快照并添加效果器。'
            return
        choice = self._vst_choice
        prior_slots = tuple(map(slot_image, (old.wire.get('inserts') or {}).get('slots', ())))
        staged = StagedInsert(old.epoch, generation, scope['slot_id'], scope['key'], prior_slots)
        self._vst_choose(binding, choice[1], choice[2], choice[3], True,
                         staged_attachment=checkpoint_attachment(checkpoint))
        swap = self._vst_swap
        swap['fx'] = staged
        candidate = swap['candidate']
        candidate.prepare_candidate_insert = partial(staged.prepare, candidate)

    def _vst_fx_can_commit(self, swap):
        if 'fx' in swap:
            swap['fx'].check(self._vst)
        return True

    def _vst_fx_committed(self, swap):
        if 'fx' not in swap:
            return
        candidate = swap['candidate']
        snapshot = swap['fx'].snapshot
        owner = swap['choice'][0].owner
        owner.update_instrument_state(snapshot['capture'], route_token=candidate.epoch,
                                      generation=snapshot['generation'])
        candidate.fx_checkpoint = snapshot
        self._editor_native_stamp = tuple(owner.head['stamp'])

    @staticmethod
    def _vst_dormant_insert(saved,scope):
        from astra.vst_host.rack_state import from_attachment,attachment
        from astra.vst_host.session import PluginSession
        from .vst_project import effect_config,application_config
        from astra.studio_bridge import PREFERENCES
        from astra.live_owner import prepare_instrument
        import base64
        path=PREFERENCES.with_name('vst-output.json')
        from astra.vst_host.rack_state import initial_slots
        rack=from_attachment(saved);rack['slots']=rack['slots'] or initial_slots()
        slots=rack['slots'];sid=scope['slot_id']
        if not isinstance(sid,str) or not sid or len(sid)>80:raise ValueError('Invalid insert slot')
        index=next((i for i,s in enumerate(slots) if s['slot_id']==sid),None)
        if index is None:
            if len(slots)>=32:raise ValueError('Insert slot capacity reached')
            index=len(slots);slots.append(dict(slot_id=sid,bypassed=False,plugin=None))
        action=scope['action']
        if action=='insert_add':
            catalog=effect_config(path);row=catalog.get(scope['key'])
            if row is None:raise ValueError('Effect is not in application catalog')
            libraries,_=application_config(path)
            session=PluginSession(libraries.library,row['module'],row['class_id'],sample_rate=48000,max_block=16384,effect=True)
            try:blob=base64.b64decode(session.request('state_get'),validate=True)
            finally:session.close()
            slots[index]['plugin']=dict(key=scope['key'],class_id=row['class_id'],state=blob)
        elif action=='insert_remove':
            slots[index]['plugin']=None
            slots[index].pop('missing',None)
        elif action=='insert_bypass':
            if type(scope.get('enabled')) is not bool:raise ValueError('Invalid bypass')
            slots[index]['bypassed']=scope['enabled']
        elif action=='insert_move':
            dest=scope['to_index']
            if type(dest) is not int or not 0<=dest<len(slots):raise ValueError('Invalid insert order')
            slots.insert(dest,slots.pop(index))
        else:raise ValueError('Unknown insert action')
        return dict(capture=prepare_instrument(attachment(rack)),generation=0)

    def _vst_finish_insert_jobs(self):
        for item in list(self._vst_insert_jobs):
            binding,route,ticket,future=item
            if not future.done():continue
            self._vst_insert_jobs.remove(item)
            try:
                value=future.result()
                if not self._vst_matches(binding) or not isinstance(route,str) and route is not self._vst:continue
                token=route if isinstance(route,str) else route.epoch
                if isinstance(route,str):
                    binding.owner.update_instrument_state(value['capture'],route_token=token,generation=value['generation'])
                    self._editor_native_stamp=tuple(binding.owner.head['stamp'])
                else:
                    binding.owner.mark_instrument_changed(token,value['state_generation'])
            except (OSError,ValueError,RuntimeError) as exc:
                from astra.vst_host.insert_control import INSERT_EXPIRED
                expired = str(exc) == INSERT_EXPIRED
                self._vst_state_warning='效果器加载超时，原效果链未变。' if expired else str(exc)
                self.logs.emit(str(exc))
            finally:binding.owner.end_instrument_capture(ticket)
        self._edit_emit_save_status()
        if not self._vst_insert_jobs and not self._vst_retiring and self._vst_pending_unmount is None and not (self._vst_pending_transport() is not None):
            while self._vst_insert_queue:
                b,scope=self._vst_insert_queue.pop(0)
                if scope['action']=='__save':
                    entry=scope['entry']
                    if not entry.closed:self._edit_enqueue_save(entry,kind=scope['kind'],retry=scope['retry'],_from_insert_queue=True)
                    break
                if self._vst_matches(b):
                    if scope['action'] in ('mount','unmount'):
                        self._open_vst_settings(json.dumps(scope),_from_insert_queue=True)
                    else:
                        try:
                            self._vst_insert_action(b,scope)
                        except (KeyError,ValueError,OSError,RuntimeError) as exc:
                            self._vst_state_warning=str(exc)
                            self.logs.emit(str(exc))
                    break

    def _vst_current_fault(self):
        fault = getattr(self, '_vst_fault', None)
        return fault if fault and self._vst_matches(fault['binding']) else None

    def _vst_can_reconnect(self):
        return (self._vst is None and self._kept_device is None and not self._vst_pending_transport()
                and not getattr(self._dm(), 'awaiting_asio_confirmation', False) and self._vst_admit())

    def _vst_remember_name(self, instrument):
        from .vst_project import remember_instrument_name
        identity = (instrument.key, instrument.class_id.lower())
        names = getattr(self, '_vst_instrument_names', {})
        if instrument.name and names.get(identity) != instrument.name:
            names[identity] = instrument.name
            self._vst_instrument_names = names
            self._vst_dispatcher.submit(remember_instrument_name, instrument)

    def _vst_valid_name(self, saved):
        from astra.vst_host.rack_state import plugin_display_name
        key, class_id = saved['key'], saved['class_id'].lower()
        name = plugin_display_name(saved.get('name'))
        if name:
            return name
        name = plugin_display_name(getattr(self, '_vst_instrument_names', {}).get((key, class_id)))
        if name:
            return name
        catalog = self._vst_catalog
        if catalog and self._vst_matches(catalog[0]):
            item = catalog[1][1].get(key)
            if item and item.class_id.lower() == class_id:
                return plugin_display_name(item.name)
        return None

    def _vst_saved_display(self):
        # Some samplers save tens of MB. Project attachments are immutable
        # bytes; decode a changed image once on the control pool. Never put
        # plugin bytes or stale project state on the UI wire.
        try:
            owner = self._vst_context().owner
            saved = owner.instrument_state()
        except (ValueError, KeyError, AttributeError):
            saved = None
        if not saved or saved.get('disabled'):
            self._vst_display_rack = None
            return None, []
        from astra.vst_host.rack_state import RACK_CLASS, RACK_KEY
        if (saved['instrument'], saved['class_id']) != (RACK_KEY, RACK_CLASS):
            return dict(key=saved['instrument'], class_id=saved['class_id']), []
        cache = getattr(self, '_vst_display_rack', None)
        if cache is None or cache[0] is not owner or cache[1] is not saved['state']:
            def read():
                from astra.vst_host.rack_state import from_attachment
                rack = from_attachment(saved)
                instrument = rack['instrument']
                return (instrument, [
                    dict(slot_id=s['slot_id'], key=(s['plugin'] or {}).get('key'),
                         bypassed=s['bypassed'], missing=bool(s.get('missing'))) for s in rack['slots']])
            if cache is not None:cache[2].cancel()
            cache = self._vst_display_rack = (owner, saved['state'], self._vst_dispatcher.submit(read))
        if cache[2].done():
            try:return cache[2].result()
            except (ValueError, KeyError, TypeError):pass
        return None, []

    def _sync_preferred_effect(self, route):
        if not route.wire.get('isolated_inserts') or self._vst_swap or self._vst_insert_jobs or self._vst_insert_queue:
            return
        pending=getattr(route,'preferred_effect_request',None)
        if pending:
            if not pending.done():
                return
            route.preferred_effect_request=None
            try:
                if pending.result() is False:
                    route.preferred_effect_applied=object()
            except (OSError,RuntimeError,ValueError) as exc:
                route.preferred_effect_error=str(exc)
        key=getattr(self,'_favorite_effect',None)
        row=self._vst_effect_catalog.get(key) if key else None
        key=key if row and row.get('available') else None
        if key==getattr(route,'preferred_effect_applied',None):
            return
        route.preferred_effect_applied=key
        route.preferred_effect_error=None
        route.preferred_effect_request=self._vst_dispatcher.submit(route.session.request,
            'prepare_favorite_effect',key=key,row=dict(module=row['module'],class_id=row['class_id']) if key else None)

    def _vst_mount_value(self):
        choice = self._vst_choice
        fault = self._vst_current_fault()
        if fault:
            instrument = fault['instrument']
            catalog = self._vst_catalog
            listed = catalog and self._vst_matches(catalog[0]) and catalog[1][1].get(instrument.key)
            can_reconnect = bool(listed and listed.class_id.lower() == instrument.class_id.lower()
                                 and self._vst_can_reconnect())
            return dict(mounted=True, name=instrument.name, key=instrument.key, active=False,
                        ready=False, connection='disconnected', can_reconnect=can_reconnect)
        if choice and self._vst_matches(choice[0]):
            active = self._vst is not None
            ready = bool(active and self._vst.ready)
            return dict(mounted=True,name=choice[1].name,key=choice[1].key,active=active,ready=ready,
                        connection='ready' if ready else 'preparing' if active else 'saved')
        saved, _ = self._vst_saved_display()
        if saved:
            attempt = saved_restore.current(self)
            return dict(mounted=True,name=self._vst_valid_name(saved),key=saved['key'],
                        active=False,ready=False,connection='preparing' if attempt and attempt.pending else 'saved',
                        can_reconnect=bool(getattr(self,'_mem_goal',None) is not None and saved_restore.can_retry(attempt,saved['key'])))
        return dict(mounted=False,name=None,active=False,ready=False)

    def _vst_mount_state(self):
        value = self._vst_mount_value()
        attempt = saved_restore.current(self)
        route = self._vst
        owns_route = bool(value['active'] and route and self._vst_matches(self._vst_binding))
        value.update(can_save_patch=bool(owns_route and route.ready and not route.playing and not route.wants_play),
                     patch_notice=route.notice if owns_route else None,
                     editor_route=route.epoch if owns_route else None,editor_open=bool(owns_route and route.editor_open),
                     editor_pending=bool(owns_route and route.editor_pending))
        catalog = self._vst_catalog
        current_catalog = catalog if catalog and self._vst_matches(catalog[0]) else None
        failures = tuple(self._vst_effect_failures.items())
        cache = getattr(self, '_vst_display_catalog', None)
        if cache is None or cache[0] is not current_catalog or cache[1] is not self._vst_effect_catalog or cache[2] != failures:
            instruments = current_catalog[1][1].values() if current_catalog else ()
            cache = self._vst_display_catalog = (current_catalog, self._vst_effect_catalog, failures,
                [dict(key=i.key,name=i.name,vendor=i.vendor,category=i.category) for i in instruments],
                [dict(key=k,name=v.get('name',k),vendor=v.get('vendor'),category=v.get('category'),
                      available=bool(v.get('available')) and k not in self._vst_effect_failures,
                      error=self._vst_effect_failures.get(k) or v.get('error')) for k,v in self._vst_effect_catalog.items()])
        value.update(catalog=cache[3],effect_catalog=cache[4],
                     busy=bool(self._vst_swap or self._vst_pending_unmount or self._vst_insert_jobs or self._vst_insert_queue
                               or (attempt and attempt.pending)),
                     catalog_loading=bool((self._vst_setup and self._vst_matches(self._vst_setup[0])) or
                                          (attempt and attempt.phase in ('lookup', 'catalog'))),
                     error=(attempt.error if attempt and attempt.phase == 'failed' and value['mounted']
                            and not value['ready'] and ((attempt.result or {}).get('identity') or (None,None))[0] == value.get('key')
                            else None) or self._vst_state_warning)
        fault = self._vst_current_fault()
        if fault and not value['error']:
            value['error'] = fault['message']
        if self._vst_swap:
            value['preparing_name'] = self._vst_swap['candidate'].instrument.name
        elif self._vst_insert_jobs:
            key = getattr(self._vst_insert_jobs[0][3], 'effect_key', None)
            if key:
                value['preparing_name'] = self._vst_effect_catalog.get(key, {}).get('name', key)
        status=(route.wire.get('inserts') or {}) if owns_route else {}
        value['preferred_effect']=getattr(self,'_favorite_effect',None)
        value['preferred_status']=status.get('favorite')
        if owns_route and getattr(route,'preferred_effect_error',None):
            value['preferred_status']=dict(key=value['preferred_effect'],phase='failed',error=route.preferred_effect_error)
        value['catalog_coverage']=getattr(self,'_vst_catalog_coverage',None)
        value['insert_latency_samples']=status.get('latency_samples')
        value['insert_latency_compensated']=False
        value['insert_editor_reason']=None if self._vst and self._vst.ready else '请先挂载乐器并等待播放路由就绪。'
        slots = status.get('slots', ()) if owns_route else self._vst_saved_display()[1]
        rows=[]
        for i,s in enumerate(slots):
            sid=s['slot_id']; key=s.get('key') if 'key' in s else (s.get('plugin') or {}).get('key')
            rows.append(dict(slot_id=sid,index=i,key=key,bypassed=s['bypassed'],missing=bool(s.get('missing')),
                editor_available=bool(owns_route and route.ready and not s.get('missing') and key),
                editor_open=bool(owns_route and route.insert_editor_open(sid,key)),
                editor_pending=bool(owns_route and (route.insert_editor_pending(sid) or s.get('editor_pending'))),
                editor_error=s.get('editor_error'),
                rack_revision=status.get('topology_revision'),
                status='dormant' if not owns_route else ('preparing' if status.get('pending_revision') else 'active')))
        value['inserts']=rows
        return value

    def _vst_legacy(self, binding):
        self._vst_unmount(binding)

    def _vst_capture(self, entry, route, *, kind='formal'):
        for _, pending_route, _, _, pending_kind in self._vst_captures:
            if pending_route is route and pending_kind == kind:
                return dict(status='queued', instrument_capture=True)
        generation = route.wire.get('state_generation', 0)
        ticket = entry.begin_instrument_capture(route.epoch, generation)
        try:
            future = route.capture_state()
        except BaseException:
            entry.instrument_capture_failed(route.epoch)
            entry.end_instrument_capture(ticket)
            raise
        self._vst_captures.append((entry, route, ticket, future, kind))
        return dict(status='queued', instrument_capture=True)

    def _vst_finish_captures(self):
        for item in list(self._vst_captures):
            entry, route, ticket, future, kind = item
            if not future.done():
                continue
            self._vst_captures.remove(item)
            succeeded = False
            try:
                value = future.result()
                generation, value = value['generation'], value['capture']
                if (kind in ('inspection', 'departure', 'initial', 'replacement', 'fx_snapshot')
                        or self._edit_beat_save_blocked(entry.project)):
                    entry.update_instrument_state(value, route_token=route.epoch, generation=generation)
                else:
                    entry.save_with_instrument(value, route_token=route.epoch, generation=generation, kind=kind)
                current = getattr(self, '_project', None) or {}
                if (current.get('path') and live_owner.retained(current['path']) is entry and
                        (current.get('editor') or {}).get('revision') == entry.head['revision']):
                    self._editor_native_stamp = tuple(entry.head['stamp'])
                succeeded = True
                route.fx_checkpoint = dict(generation=generation, capture=value)
                if getattr(route,'fx_after_snapshot',None) and route is self._vst:
                    self._vst_insert_queue[0:0] = route.fx_after_snapshot
                    route.fx_after_snapshot = []
                route.capture_failed = False
                nav=getattr(self,'_nav_parking',None)
                if kind=='departure' and nav is getattr(route,'navigation_park',None) and nav is not None:
                    nav['attachment']=entry.instrument_state()
                    nav['capture_generation']=generation
            except (OSError, ValueError, RuntimeError) as exc:
                entry.instrument_capture_failed(route.epoch)
                entry.saves.capture_error('recovery' if kind=='recovery' else 'formal', exc)
                route.capture_failed = True
                if kind == 'fx_snapshot':
                    route.fx_after_snapshot = []
                route.notice = str(exc)
                self._vst_state_warning = str(exc)
                self.logs.emit(str(exc))
            finally:
                entry.end_instrument_capture(ticket)
                if succeeded and (kind=='departure' or route in self._vst_retiring):
                    route.retire()
        self._edit_emit_save_status()

    def _edit_enqueue_save(self, entry, *, kind='formal', retry=False, _from_insert_queue=False):
        if self._edit_beat_save_blocked(entry.project):
            return dict(status='skipped', error='请先确认或取消录拍。')
        if not _from_insert_queue and (self._vst_insert_jobs or self._vst_insert_queue or self._vst_swap is not None or self._vst_pending_unmount is not None):
            self._vst_insert_queue.append((None,dict(action='__save',entry=entry,kind=kind,retry=retry)))
            return dict(status='queued',instrument_capture=True)
        if retry and entry.saves.status().get(kind, {}).get('failed_save'):
            return super()._edit_enqueue_save(entry, kind=kind, retry=True)
        routes = ([self._vst] if self._vst is not None else []) + self._vst_retiring
        route = next((r for r in routes if getattr(r,'capture_owner',None) is entry and r.ready),None)
        if route is not None:
            if kind == 'recovery' and not retry and (route.playing or route.wants_play):
                route.recovery_capture_deferred = True
                return entry.backup(keep_last_instrument=True)
            return self._vst_capture(entry, route, kind=kind)
        return super()._edit_enqueue_save(entry, kind=kind, retry=retry)

    def _vst_deferred_recovery(self, route):
        if (not getattr(route, 'recovery_capture_deferred', False) or not route.capture_bound
                or self._edit_beat_save_blocked(route.capture_owner.project)):
            return
        if (route.playing or route.wants_play or self._vst_pending_transport()
                or self._vst_captures or self._vst_insert_jobs or self._vst_insert_queue):
            route.recovery_paused_at = None
            return
        now = route.now()
        paused_at = getattr(route, 'recovery_paused_at', None)
        if paused_at is None:
            route.recovery_paused_at = now
            return
        if now-paused_at < .35:
            return
        route.recovery_capture_deferred = False
        route.recovery_paused_at = None
        try:
            if route.capture_owner.saves.status()['recovery_enabled']:
                self._vst_capture(route.capture_owner, route, kind='recovery')
        except (OSError, ValueError, RuntimeError) as exc:
            route.capture_owner.saves.capture_error('recovery', exc)

    def _edit_has_pending_vst_work(self, entries):
        self._vst_pump()
        return bool(
            self._vst_captures
            or self._vst_insert_jobs
            or self._vst_insert_queue
            or self._vst_swap is not None
            or self._vst_pending_unmount is not None
        )

    def _edit_prepare_save_inspection(self, entries):
        super()._edit_prepare_save_inspection(entries)
        swap = self._vst_swap
        if swap is not None and swap['old'] is None:
            candidate = swap['candidate']
            # The first instrument has not published any state to the project.
            # Don't wait for a sound card to open just to inspect saved edits.
            if not candidate.ready and not getattr(candidate, 'capture_bound', False):
                saved_restore.abandon(self)
                self._vst_cancel_swap()
                if self._vst is candidate:
                    self._vst_leave(capture=False, reason='application_closing')
        routes = ([self._vst] if self._vst is not None else []) + self._vst_retiring
        for route in routes:
            if getattr(route,'capture_owner',None) in entries and route.ready:
                try:
                    if route.playing or route.wants_play:
                        route.transport('pause')
                    self._vst_capture(route.capture_owner, route, kind='inspection')
                except (OSError, ValueError, RuntimeError) as exc:
                    route.capture_owner.instrument_capture_failed(route.epoch)
                    route.capture_owner.saves.capture_error('formal', exc)
                    self.logs.emit(str(exc))

    def _vst_admit(self):
        # Same retirement proof the existing memory route requires. When Tracks
        # already runs on the shared KeptEndpoint, both lanes share that single
        # physical ASIO stream without closing or reopening the device.
        shared = self._output_shares_kept_endpoint()
        shared_owner = self._output.owner if shared and self._output else None
        return (not self._output_shutdown and not self._vst_retiring and (self._output is None or shared) and
                not self._output_retiring and self._mem_owner is None and self._mem_retiring is None and
                getattr(self, '_asio_panel', None) is None and
                all(owner is shared_owner or ((s := owner.snapshot()).closed and s.output_released)
                    for owner in self._output_owners))

    def _plugin_startup(self):
        if self._plugin_startup_scheduled or self._closed:return
        self._plugin_startup_scheduled=True
        if self._plugin_scan is None and not self._plugin_scan_status.get('initialized'):
            self._plugin_start(auto=True)

    def _plugin_start(self, *, auto):
        if self._closed or self._plugin_scan is not None:return
        from threading import Thread
        self._plugin_scan_auto=auto
        self._plugin_scan_status=dict(running=True,completed=0,automatic=auto)
        task=Future();self._plugin_scan=task
        def progress(value):
            if self._closed:raise RuntimeError('Plugin scan cancelled: application closed')
            self._plugin_scan_status=dict(value,running=True,automatic=auto)
        def work():
            try:
                from .vst_metadata import refresh_catalog, scan_catalog
                from .vst_project import _application_data
                from astra.studio_bridge import PREFERENCES
                progress(dict(completed=0))
                library=_application_data(PREFERENCES.with_name('vst-output.json'))['library']
                rows, cov = scan_catalog()
                try:self._vst_dispatcher.done.emit()
                except RuntimeError:pass
                if auto and not cov.get('unresolved'):
                    task.set_result((rows, cov))
                else:
                    task.set_result(refresh_catalog(library,progress,retry_failed=not auto))
            except BaseException as exc:task.set_exception(exc)
            try:self._vst_dispatcher.done.emit()
            except RuntimeError:pass
        # No global Qt pool shutdown wait; the scanner itself has a bounded timeout.
        Thread(target=work,name='VST3 inventory',daemon=True).start()

    def _plugin_finish(self):
        pending=self._plugin_scan
        if pending is None or not pending.done():return
        self._plugin_scan=None
        try:
            _,report=pending.result()
            self._plugin_scan_status=dict(report,running=False,initialized=True)
            self._vst_refresh_open_catalog()
        except Exception as exc:
            self._plugin_scan_status=dict(running=False,initialized=True,error=str(exc))
        retry=self._plugin_scan_retry;self._plugin_scan_retry=False
        if retry and not self._closed:self._plugin_start(auto=False)

    def _plugin_manager(self, message):
        action=json.loads(message).get('action','status')
        if action not in ('status','refresh'):raise ValueError('Unknown plugin manager action')
        self._plugin_finish()
        if action=='refresh' and not self._closed:
            if self._plugin_scan is None:self._plugin_start(auto=False)
            elif self._plugin_scan_auto:self._plugin_scan_retry=True
        from .vst_metadata import static_catalog
        rows,_=static_catalog()
        result=dict(self._plugin_scan_status)
        result['inventory']=[dict(id=r.cid,name=r.name,kind=r.kind,vendor=r.vendor,category=r.category,module=Path(r.module).name) for r in rows]
        return json.dumps(result,ensure_ascii=False)

    def _vst_release_previous(self):
        if self._previous_instrument:
            session = self._previous_instrument[-1]
            self._previous_instrument = None
            self._vst_dispatcher.submit(session.close)

    def _vst_check_endpoint(self, route):
        endpoint = self._kept_device
        if not route.ready or endpoint is None or 'kept_endpoint' not in route.device:
            return
        now = perf_counter()
        if now < getattr(route, 'next_endpoint_check', 0):
            return
        route.next_endpoint_check = now + .2
        try:
            endpoint.poll()
        except (ValueError, OSError, RuntimeError) as exc:
            self._vst_observe_runtime(route, endpoint, now, trigger=False)
            route.fail(exc, stage='device_stream')
            return
        self._vst_observe_runtime(route, endpoint, now)

    def _vst_observe_runtime(self, route, endpoint, now, *, trigger=True):
        # This runs on the control thread at no more than 5 Hz. The existing
        # native counter reads do not alter timing or request work from the worker.
        read = getattr(endpoint, 'diagnostic_state', None)
        device = read() if read is not None else None
        if device is None:
            return
        samples = getattr(route, 'runtime_samples', None)
        if samples is None:
            samples = route.runtime_samples = deque(maxlen=24)
            route.runtime_report_at = {}
        previous = samples[-1] if samples else None
        worker_clock = route.now()
        process = getattr(route.session, 'process', None)
        next_pending = route.flight if route.flight is not None else route._insert_flight
        pending_since = (getattr(route, 'flight_started', None) if route.flight is not None
                         else getattr(route, 'insert_flight_started', None))
        last_status = getattr(route, 'last_worker_status_at', None)
        status_age = int(1000 * max(0., worker_clock-last_status)) if last_status is not None else None
        gap = round(1000 * (now-previous['monotonic'])) if previous else None
        requested = route.device['kept_endpoint']['token']
        request = getattr(self, '_request', None) or {}
        row = dict(monotonic=round(now, 6), controls_gap_ms=gap, endpoint=device,
                   worker_pid=getattr(process, 'pid', None), playing=route.playing,
                   worker_ready=route.ready, status_age_ms=status_age,
                   position_frames=(route.wire.get('transport') or {}).get('position_samples'),
                   worker_underruns=route.wire.get('underruns'), parked=bool(route.wire.get('control_parked')),
                   ipc=next_pending[0] if next_pending else None,
                   ipc_age_ms=int(1000 * max(0., worker_clock-pending_since)) if next_pending and pending_since is not None else None,
                   captures=[kind for _, owner, _, _, kind in self._vst_captures if owner is route],
                   requested_token=requested,
                   bound_choice=bool(self._vst_choice and self._vst_choice[1] == route.instrument),
                   ui_revision=getattr(self, '_vst_view_revision', None), ui_ack=getattr(self, '_vst_view_ack', None),
                   editor_export_active=bool(request.get('editor_revision')
                                             and getattr(getattr(self, 'jobs', None), 'running', False)))
        samples.append(row)
        if not trigger or not previous or not row['playing'] or not previous['playing']:
            return
        delta = device['dropped_blocks'] - previous['endpoint']['dropped_blocks']
        if (delta>0 and device['selected_token']==requested
                and previous['endpoint']['selected_token']==requested):
            self._vst_report_runtime(route, 'ring_underrun', f'{delta} missing output blocks since the preceding sample', now)
        elif gap is not None and gap>=900:
            self._vst_report_runtime(route, 'control_loop_gap', f'{gap} ms between control samples', now)
        elif status_age is not None and status_age>=900 and next_pending:
            self._vst_report_runtime(route, 'worker_status_gap', f'No new worker status for {status_age} ms', now)

    def _vst_report_runtime(self, route, stage, detail, now):
        previous = route.runtime_report_at.get(stage)
        if previous is not None and now-previous<12:
            return
        route.runtime_report_at[stage] = now
        self._vst_write_runtime(route, stage, detail)

    def _vst_write_runtime(self, route, stage, detail):
        self._vst_dispatcher.submit(self._vst_record_fault, stage, route.instrument.name,
                                    route.device.get('backend'), detail,
                                    route_epoch=route.epoch,
                                    samples=tuple(getattr(route, 'runtime_samples', ())))

    def _vst_disconnect(self, route, binding):
        stage = getattr(route, 'failure_stage', None)
        label = {'worker_exit':'工作进程退出','timeout':'通信超时','protocol':'通信异常',
                 'device_stream':'音频端点中断','rt_status':'状态读取失败'}.get(stage,'播放中断')
        message = f'{route.instrument.name} 已断开（{label}）。{route.error}'
        self._vst_fault = dict(binding=binding, instrument=route.instrument, position=route.position,
                               duration=route.duration, message=message)
        self._vst_state_warning = None
        self._vst_leave(capture=False)
        self.logs.emit(message)
        self._vst_write_runtime(route, stage, route.error)

    @staticmethod
    def _vst_record_fault(stage, name, backend, error, *, route_epoch=None, samples=()):
        from datetime import datetime, timezone
        from astra.studio_bridge import PREFERENCES
        path = PREFERENCES.with_name('vst-runtime-events.jsonl')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            row = dict(at=datetime.now(timezone.utc).isoformat(), stage=stage, instrument=name,
                       backend=backend, error=str(error)[:512], route_epoch=route_epoch, samples=samples)
            with _runtime_log_lock:
                if path.exists() and path.stat().st_size > 256 * 1024:
                    path.replace(path.with_suffix('.previous.jsonl'))
                with path.open('a', encoding='utf8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        except OSError:
            pass

    def _vst_pump(self):
        names = getattr(self, '_vst_instrument_names_load', None)
        if names and names.done():
            self._vst_instrument_names_load = None
            try:
                self._vst_instrument_names = {**names.result(), **self._vst_instrument_names}
            except (OSError, ValueError):
                pass
        if self._previous_instrument and perf_counter() >= self._standby_memory_check:
            from .kept_endpoint import standby_memory_available
            self._standby_memory_check = perf_counter() + 2.
            if perf_counter() >= self._previous_instrument[-2] or not standby_memory_available():
                self._vst_release_previous()
        self._plugin_finish()
        self._vst_finish_insert_jobs()
        self._vst_finish_captures()
        if self._vst_setup is not None and self._vst_setup[1].done():
            b, task = self._vst_setup
            self._vst_setup = None
            if not task.cancelled() and not self._closed and self._vst_matches(b):
                try:
                    catalog,effects,*coverage=task.result()
                    self._vst_catalog_coverage=coverage[0] if coverage else None
                    self._vst_catalog = b,catalog
                    self._vst_effect_catalog=effects
                    self._vst_effect_failures={}
                except (ValueError, OSError, RuntimeError) as exc:
                    self._vst_state_warning = str(exc)
                    self.logs.emit(str(exc))
        r, b = self._vst, self._vst_binding
        if r is not None and not self._vst_matches(b):
            if r.ready:
                self._vst_write_runtime(r, 'binding_invalidated', 'Active VST route lost its editor binding')
            self._vst_leave()
            r = None
        if self._vst_choice is not None and not self._vst_matches(self._vst_choice[0]):
            self._vst_choice = None
        if getattr(self, '_vst_fault', None) and self._vst_current_fault() is None:
            self._vst_fault = None
        for old in self._vst_retiring:
            old.pump()
            parked = getattr(old, 'parked_session', None)
            if parked is not None and getattr(old,'navigation_receipt',None):
                old.parked_session=None
                slot=getattr(self,'_nav_parking',None)
                if slot is getattr(old,'navigation_park',None) and slot is not None:
                    slot.update(session=parked,receipt=old.navigation_receipt)
                else:
                    self._vst_dispatcher.submit(parked.close)
                parked=None
            if parked is not None:
                old.parked_session = None
                owner = getattr(old, 'standby_binding', None)
                self._vst_release_previous()
                if (self._vst is not None and self._vst_swap is None and
                        self._kept_device and self._vst_matches(owner) and not self._closed):
                    self._previous_instrument = (owner, old.instrument, old.libraries, self._kept_device,
                                                 perf_counter() + 120, parked)
                else:
                    self._vst_dispatcher.submit(parked.close)
            endpoint=getattr(old,'departing_endpoint',None)
            if old.released and endpoint is None and self._kept_device is not None:
                self._kept_device.forget_retired(old)
            if old.released and endpoint and endpoint.handle:
                endpoint.close()
                endpoint.poll()
                if not endpoint.handle and self._kept_device is endpoint:
                    self._kept_device=None
        self._vst_retiring[:] = [old for old in self._vst_retiring if not old.released or
                                 getattr(getattr(old,'departing_endpoint',None),'handle',None)]
        if (self._kept_device is not None and self._kept_device.closing and not self._vst_retiring
                and not getattr(self, '_output_retiring', ())):
            try:
                self._kept_device.poll()
            except Exception:
                pass
            if not self._kept_device.handle:
                self._kept_device = None
        slot=getattr(self,'_nav_parking',None)
        if slot and slot['route'].released and not slot.get('session') and slot['route'] not in self._vst_retiring:
            self._nav_parking_discard()
            if slot['returning']:self._vst_maybe_restore_saved(slot['choice'][0].uri)
        self._nav_parking_tick()
        self._vst_panel_tick()
        saved_restore.tick(self)
        self._vst_swap_tick()
        saved_restore.tick(self)
        r, b = self._vst, self._vst_binding
        self._vst_finish_unmount()
        if r is not None:
            try:
                if not r.retiring and not getattr(r,'_score_source',False):
                    h = b.owner.head
                    review = self._review_click_for_head(h) if hasattr(self, '_review_click_for_head') else None
                    review_seq = review['seq'] if review is not None else None
                    if r.wanted is None or h['revision'] != getattr(r, 'music_revision', None) or review_seq != getattr(r, 'review_click_seq', None):
                        frozen=b.owner.freeze(h['stamp'])
                        if review is not None:
                            from .vst_score_snapshot import WireReviewNotes
                            seq=max(h['applied_sequence'],r.wanted if r.wanted is not None else -1)+1
                            frozen=WireReviewNotes(frozen,seq,review['table'])
                        r.offer(frozen)
                        r.music_revision = h['revision']
                        r.review_click_seq = review_seq
                    loop = h['range']
                    values = (loop['start'], loop['end'], loop['loop'])
                    if self._vst_range != values:
                        self._vst_range = values
                        self._audition_range = None
                        r.set_loop(*values)
                mix = self._vst_mix()
                r.set_mix(mix)
                self._vst_check_endpoint(r)
                r.pump(self._vst_admit())
                if r.rate_error and not r.restoring_notes_rate and not getattr(r,'_score_source',False) and self._vst_pending_transport() is None and r.requested_rate != getattr(self, '_audition_rate', 1.):
                    self._audition_rate = r.requested_rate
                    self._output_desired = replace(self._output_desired, rate=r.requested_rate)
                    self._publish()
                self._vst_finish_together(r)
                if r.ready:
                    self._sync_preferred_effect(r)
                    generation = r.wire.get('state_generation', 0)
                    if not r.capture_bound:
                        if self._vst_choice and self._vst_choice[1] == r.instrument:
                            self._vst_remember_name(r.instrument)
                        identity = (r.instrument.key, r.instrument.class_id.lower())
                        saved = getattr(r, 'restored_instrument_identity', (None, None))
                        changed = saved != identity and r.loaded_legacy_state is None
                        r.capture_owner.bind_instrument_route(r.epoch, generation, changed=changed, baseline=r.loaded_legacy_state)
                        r.capture_bound = True
                        capture=getattr(r,'prepared_capture',None)
                        if getattr(r, 'saved_reopen', None) is not None:
                            # A successful reopen is not a plugin edit. Keep
                            # the exact project image as the baseline.
                            r.fx_checkpoint = dict(generation=generation, capture=r.instrument_state)
                        elif capture and capture['generation']==generation:
                            r.fx_checkpoint=capture
                            r.capture_owner.update_instrument_state(capture['capture'],route_token=r.epoch,generation=generation)
                            self._editor_native_stamp=tuple(r.capture_owner.head['stamp'])
                            r.prepared_capture=None
                        else:
                            self._vst_capture(r.capture_owner, r, kind='initial')
                    r.capture_owner.mark_instrument_changed(r.epoch, generation)
                    if (getattr(r,'fx_after_snapshot',None) and not r.wants_play and not r.playing
                            and not self._vst_pending_transport() and not self._vst_captures):
                        self._vst_capture(r.capture_owner, r, kind='fx_snapshot')
                    else:
                        self._vst_deferred_recovery(r)
            except (KeyError, ValueError, OSError, RuntimeError) as exc:
                r.fail(exc)
            if r.error and self._vst_swap is None and not getattr(self, '_vst_panel_transition', None):
                self._vst_disconnect(r, b)
                r = None
        readback_route=r
        if readback_route is None and self._vst_swap is not None:
            readback_route=self._vst_swap.get('candidate')
        signature = (self._vst_mount_state(), self._vst_retirement_message(),
                     bool(getattr(self, '_vst_page_suspended', False)),
                     self._vst_driver_readback(readback_route), None if r is None else (
            r.epoch, r.wire, r.wanted, r.submitted, r.duration, r.wants_play, r.requested_position,
            r.error, r.projection_error, r.notice, r.requested_rate, r.rate_error, self._vst_feedback))
        if signature != self._vst_signature:
            self._vst_signature = signature
            if not self._closed:
                self._player_tick()
                self._notify_midi()

    def _vst_retirement_message(self):
        if self._vst_retiring:
            return next((r.notice for r in self._vst_retiring if r.notice), 'VST 输出尚未释放。')
        return None

    def _output_poll(self):
        endpoint = getattr(self, '_kept_device', None)
        if (getattr(self, '_settings_asio_handoff', None) is None
                and endpoint is not None and endpoint.handle and not endpoint.closing
                and endpoint.info is None and not self._output_shutdown):
            try:
                endpoint.poll()
            except Exception:
                # Startup pre-warm failure without an active owner is deferred until
                # an explicit project open or Play requests the endpoint.
                if self._output is None and self._vst is None and self._vst_swap is None:
                    try:
                        endpoint.close()
                    except Exception:
                        pass
        super()._output_poll()
        self._pump_settings_asio_handoff()
        self._vst_pump()
        transport_pages.poll_score(self)
        transport_pages.poll_builtin(self)
        self._vst_apply_editor_space()

    def _vst_apply_editor_space(self):
        route = self._vst_active()
        take = getattr(getattr(route, 'session', None), 'take_editor_space', None)
        if take is None:
            return
        for event in take():
            if (transport_pages.files_active(self) or self._closed or self._output_shutdown or getattr(self, '_editor_closing', False)
                    or self._vst_active() is not route or not route.ready
                    or not self._vst_matches(self._vst_binding)
                    or event.scope != route.lane_id or not event.current()):
                continue
            try:
                # Keep the active audition source, cursor, loop and pending
                # handoff. This is the same transport arbiter as Studio's Space.
                if not getattr(route, '_score_source', False) and self._notes_transport_binding() is not None:
                    self._toggle_midi_output('midi')
                else:
                    self._output_change(playing=not self._output_intent().playing)
            except (OSError, ValueError, RuntimeError) as exc:
                self.logs.emit(str(exc) or type(exc).__name__)

    def _native_mode(self, audition, rate, metronome=None):
        if self._vst_active() is None and audition == 'joint':
            # Unbound joint is audio-only; its handoff mode must match the actual device owner.
            return super()._native_mode('audio', rate, metronome)
        if audition == 'midi' and self._vst_active() is None:
            return 'native-midi'
        return super()._native_mode(audition, rate, metronome)

    def _output_ensure(self, audition=None):
        if getattr(self, '_settings_asio_handoff', None) is not None:
            return None
        if (self._vst_active() is not None or self._vst_retiring or getattr(self,'_vst_panel_transition',None)
                or (getattr(getattr(self,'_kept_device',None),'handle',None) and getattr(self._kept_device,'closing',False))):
            return None
        return super()._output_ensure(audition=audition)

    def _memory_supported(self):
        # An unbound track is not dependent on a selected output driver.
        return True if self._vst is None else super()._memory_supported()

    def _memory_available(self):
        if self._vst is None:
            return not self._output_shutdown
        return super()._memory_available()

    def _try_memory_start(self):
        if getattr(self,'_vst_panel_transition',None):return
        if self._vst is None and not self._vst_retiring:
            from .native_midi import SilentMidiSession
            self._mem_factory = SilentMidiSession
            return super()._try_memory_start()

    def _vst_promote_background_swap(self, value, resume=None):
        swap = self._vst_swap
        if swap is None or not swap.get('background_suspend'):
            attempt = saved_restore.current(self)
            if (attempt is not None and attempt.background and attempt.pending
                    and attempt.swap is None and attempt.binding.uri == value):
                previous = getattr(self, '_vst_internal_midi_clear', None)
                self._vst_internal_midi_clear = attempt
                try:
                    super()._select_live_editor(value, resume)
                finally:
                    self._vst_internal_midi_clear = previous
                saved_restore.start(self, attempt.binding, background=False)
                self._notify_midi()
                return True
            return False
        binding = swap['choice'][0]
        if binding.uri != value or not self._vst_matches(binding):
            saved_restore.abandon(self, include_background=True)
            self._vst_cancel_swap()
            return False
        swap['background_suspend'] = False
        saved = swap.get('saved_reopen')
        if saved is not None:
            saved.background = False
        if swap['phase'] == 'opening' and self._vst is swap['candidate']:
            self._pause_shared_tracks_for_vst()
            self._vst_page_suspended = False
            self._midi_file = dict(path=binding.uri, name='编辑版本', notes=[])
            if resume is not None and not (saved and saved.play_requested and swap['playing']):
                self._vst_swap_intent(dict(position=resume[0], playing=resume[1]))
            self._notify_midi()
            return True
        # Still loading plugin before _vst_enter: set foreground silent editor state
        # so Notes UI and transport bindings stay consistent while load finishes.
        previous = getattr(self, '_vst_internal_midi_clear', None)
        self._vst_internal_midi_clear = swap
        try:
            super()._select_live_editor(value, resume)
        finally:
            self._vst_internal_midi_clear = previous
        if resume is not None and not (saved and saved.play_requested and swap['playing']):
            self._vst_swap_intent(dict(position=resume[0], playing=resume[1]))
        self._notify_midi()
        return True

    def _select_live_editor(self, value, resume=None):
        restore = transport_pages.early_notes(self, value)
        if restore is not None and resume is None:
            resume = (restore, False)
        if self._vst_promote_background_swap(value, resume):
            return
        pending=self._vst_pending_transport()
        if pending is not None and not self._vst_matches(pending['choice'][0]):
            saved_restore.abandon(self)
            self._vst_cancel_swap()
            pending=self._vst_pending_transport()
        if pending is not None and pending['choice'][0].uri == value:
            saved=pending.get('saved_reopen')
            if resume is not None and not (saved and saved.play_requested and pending['playing']):
                self._vst_swap_intent(dict(position=resume[0],playing=resume[1]))
            return
        lookup=saved_restore.lookup(self)
        if lookup and lookup.play_requested and lookup.binding.uri==value:
            # A relink of this same Notes document is not an explicit Pause.
            return
        # The retained choice must not start a second worker during capture or close.
        slot = getattr(self, '_nav_parking', None)
        if slot and slot['choice'][0].uri == value and self._vst_matches(slot['choice'][0]):
            slot['returning'] = True
            if resume is not None:
                slot['position'] = resume[0]
            return
        choice = self._vst_choice
        if choice and choice[0].uri == value and self._vst_matches(choice[0]):
            if self._vst is None and self._vst_current_fault() is None:
                self._vst_enter(choice, resume[0] if resume else 0.)
                self._publish()
            elif self._vst is not None and (self._midi_file or {}).get('path') != value:
                self._midi_file = dict(path=choice[0].uri, name='编辑版本', notes=[])
                self._notify_midi()
            # Refresh while the same route is selected is not a Play or a seek.
            return
        result = super()._select_live_editor(value, resume)
        self._vst_maybe_restore_saved(value)
        return result

    def _vst_maybe_restore_saved(self, value):
        if value != self._editor_uri or self._vst is not None or self._vst_pending_transport() is not None:
            return
        try:
            slot=getattr(self,'_nav_parking',None)
            if slot and value==slot['choice'][0].uri:
                slot['returning']=True
                return
            if saved_restore.start(self, self._vst_context()):
                self._notify_midi()
        except (KeyError, ValueError, RuntimeError):
            # A document replaced while working MIDI was selected cannot
            # authorize starting a plugin on the newly opened project.
            return

    def _vst_mount_builtin_for_score(self, binding):
        """The Score page has no instrument: mount MuseScore's own piano.

        Returns None to be asked again later, True once mounting started,
        False when it can never apply (the built-in module is missing).

        MuseScore always plays a score with its built-in FluidSynth sound. Do
        the same here, through the normal instrument admission and the global
        output endpoint, whenever no instrument is mounted (none connecting and
        no saved rack still restoring). It is a Score-only route: the editor's
        saved instrument attachment is neither loaded nor overwritten."""
        if (self._closed or self._output_shutdown or self._vst is not None or self._vst_swap is not None
                or self._vst_pending_transport() is not None or not self._vst_matches(binding)
                or getattr(self, '_asio_panel', None) is not None):
            return None   # not now; asked again on the next poll
        attempt = saved_restore.current(self)
        if attempt is not None and attempt.pending:
            return None
        manager = self._dm()
        if getattr(manager, 'awaiting_asio_confirmation', False):
            return None
        from .vst_project import application_config, endpoint_choices, BUILTIN_KEY
        from . import device_catalog
        from astra.studio_bridge import PREFERENCES
        libraries, instruments = application_config(PREFERENCES.with_name('vst-output.json'))
        instrument = instruments.get(BUILTIN_KEY)
        if instrument is None:
            return False
        device = global_device_route(manager.current, endpoint_choices(device_catalog.catalog()),
                                     active_endpoint=self._kept_device)
        self._vst_choose(binding, instrument, libraries, device, True)
        self._notify_midi()
        return True

    def _vst_reopen_silent_after_saved_failure(self, binding, position):
        if self._vst is None and self._mem_goal is None and self._vst_matches(binding):
            try:
                super()._select_live_editor(binding.uri, resume=(position, False))
            except (KeyError, ValueError, RuntimeError) as exc:
                self.logs.emit(str(exc))

    def _midi_selection_resume(self, resume):
        # File/view reconstruction cannot transfer a VST Play to VMS.
        if self._vst_retiring and resume is not None:
            resume = (resume[0], False)
        return super()._midi_selection_resume(resume)

    def _clear_midi(self):
        self._vst_leave(reason='midi_selection_cleared')
        return super()._clear_midi()

    def _suspend_midi_output(self):
        attempt = saved_restore.current(self)
        swap = attempt.swap if attempt else None
        internal = getattr(self, '_vst_internal_midi_clear', None)
        if not ((swap is not None and swap is internal) or (attempt is not None and attempt is internal)):
            saved_restore.abandon(self)
            if swap is not None and swap is self._vst_swap:
                self._vst_cancel_swap()
        self._vst_leave(reason='midi_output_suspended')
        return super()._suspend_midi_output()

    def _select_audio(self, key, preserve=True):
        if key is None:
            self._notes_transport_session = None
            saved_restore.abandon(self)
            self._vst_cancel_swap()
            self._vst_leave(reason='audio_source_selected')
            return super()._select_audio(key, preserve)
        if any(row['key'] == key for row in self._sources):
            self._notes_transport_session = None
            saved_restore.abandon(self, include_background=False)
            if self._vst_swap is not None and not self._vst_swap.get('background_suspend'):
                self._vst_cancel_swap()
            if self._vst is not None and self._kept_device is not None and not self._kept_device.closing:
                # Keep the mounted VST warm and paused on the shared ASIO endpoint while Tracks plays.
                if not getattr(self, '_vst_page_suspended', False):
                    self._vst_suspend_for_tracks()
                else:
                    self._kept_device.set_direct_mode(True)
            else:
                self._vst_leave(reason='audio_source_selected')
        return super()._select_audio(key, preserve)

    def _hold_score_for_files(self, path):
        return transport_pages.hold_score_for_files(self, path)

    def _navigate_transport_page(self, data):
        transport_pages.navigate(self, data)

    def _select_audition_source(self, data):
        if data.get('leaveNativeTracks') is True:
            data=self._pause_native_tracks_for_navigation(data)
        data = transport_pages.arrival(self, data)
        if data is None:
            return
        if data['key'] == 'midi' and self._vst_swap is not None and self._vst_swap.get('background_suspend'):
            pos = 0. if data.get('preservePosition') else data.get('position', 0.)
            self._vst_promote_background_swap(self._editor_uri, resume=(pos, False))
        pending = self._vst_pending_transport()
        if pending is not None and data['key']=='midi':
            loop=data.get('range') or {}
            delta=dict(loop=(loop.get('start',0),loop.get('end',0),loop.get('loop',False)))
            if not data.get('preservePosition'):
                if data.get('restoreNotesPosition'):
                    if not pending['playing']:
                        delta.update(playing=False,position=data['position'])
                elif pending.get('saved_reopen') and data.get('navigation') is True:
                    # Notes sends a paused MIDI selection immediately after
                    # selecting the same working file. It isn't a user pause.
                    if not pending['playing'] and 'position' not in pending['explicit']:
                        delta['position'] = data['position']
                else:
                    delta.update(playing=data['playing'],position=data['position'])
            return self._output_change(**delta)
        if data['key'] != 'midi':
            self._notes_transport_session = None
            saved_restore.abandon(self, include_background=False)
            cancel = getattr(self, '_vst_cancel_swap', None)
            if cancel and not (self._vst_swap and self._vst_swap.get('background_suspend')):
                cancel()
            slot=getattr(self,'_nav_parking',None)
            if slot:slot['returning']=False
            departing = self._vst is not None and not getattr(self, '_vst_page_suspended', False)
            if departing and any(row['key'] == data['key'] for row in self._sources):
                endpoint = getattr(self, '_kept_device', None)
                if endpoint is not None and not endpoint.closing and self._vst_matches(self._vst_binding):
                    # Shared ASIO endpoint: pause and suspend VST in place without closing
                    # the ASIO stream or unloading the instrument.
                    self._vst_suspend_for_tracks()
                else:
                    reason = 'page_navigation' if data.get('navigation') else 'audition_source_selected'
                    self._vst_leave(reason=reason)
                data = dict(data, playing=False)
            elif getattr(self, '_kept_device', None) is not None and not self._kept_device.closing:
                self._kept_device.set_direct_mode(True)
            result = super()._select_audition_source(data)
            self._player_tick()
            self._notify_midi()
            return result
        slot = getattr(self, '_nav_parking', None)
        if (self._vst is None and slot and data.get('navigation') and not data['playing']
                and slot['choice'][0].uri == self._editor_uri and self._vst_matches(slot['choice'][0])):
            # Paused Notes arrival must not open a fallback MIDI output during teardown.
            slot['returning'] = True
            if not data.get('preservePosition'):
                slot['position'] = data['position']
            return
        if self._vst is None and self._mem_goal is None and (self._midi_file or {}).get('path') == self._editor_uri:
            self._select_live_editor(self._editor_uri, resume=(data['position'], False))
        if self._vst is None:
            result = super()._select_audition_source(data)
            self._vst_maybe_restore_saved(self._editor_uri)
            return result
        if getattr(self, '_vst_page_suspended', False):
            # Resume foreground ownership on the already-mounted VST without reopening ASIO.
            self._pause_shared_tracks_for_vst()
            self._vst_page_suspended = False
        loop = data.get('range') or {}
        delta = dict(loop=(loop.get('start', 0), loop.get('end', 0), loop.get('loop', False)))
        if not data.get('preservePosition'):
            delta.update(playing=data['playing'], position=data['position'])
        result = self._output_change(**delta)
        self._player_tick()
        self._notify_midi()
        return result

    def _vst_pcm_source(self):
        row = next((row for row in self._sources if row['key'] == self._source),None)
        if row is None:
            raise ValueError('No selected audio source for VST joint playback')
        path = Path(row['path']).resolve()
        stat = path.stat()
        return dict(key=row['key'],path=str(path),
                    identity=dict(size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                                  source_version=self._sources_version),offset_seconds=0.)

    def _output_intent(self):
        pending=self._vst_pending_transport()
        r = self._vst_active()
        if r is None and pending is not None and not getattr(self, '_vst_page_suspended', False):
            return replace(self._output_desired,mode='vst',audition='joint' if pending.get('pcm') is not None else 'midi',
                           playing=pending['playing'],position=pending['position'],rate=getattr(self, '_audition_rate', 1.))
        if r is None:
            return super()._output_intent()
        return replace(self._output_desired, mode='vst', audition='joint' if r.pcm_source is not None else 'midi', playing=r.wants_play,
                       position=r.requested_position if not getattr(r, '_score_source', False) else r.position,
                       midi_duration=r.duration, rate=r.requested_rate, loop=r.loop or (0.,0.,False))

    def _output_change(self, **delta):
        notes_page_seek=delta.pop('_notes_page_seek',False)
        # Retired saved/deferred joint intents become MIDI-only, never open PCM.
        if delta.get('audition') == 'joint':
            delta = dict(delta, audition='midi')
        if delta.get('audition') == 'audio':
            saved_restore.abandon(self, include_background=False)
            cancel = getattr(self, '_vst_cancel_swap', None)
            if cancel and not (self._vst_swap and self._vst_swap.get('background_suspend')):
                cancel()
        if delta.get('audition') == 'midi' and self._vst is not None and getattr(self, '_vst_page_suspended', False):
            self._pause_shared_tracks_for_vst()
            self._vst_page_suspended = False
        attempt = saved_restore.current(self)
        if attempt and not attempt.background and attempt.phase in ('lookup','catalog') and delta.get('audition','midi')=='midi':
            if delta.get('playing') is False:
                attempt.play_requested=False
            if delta.get('playing') is True:
                # Only an explicit Notes command grants Play.
                self._notify_midi()
                return
        pending=self._vst_pending_transport()
        if attempt and not attempt.background and pending is not None and attempt.swap is pending:
            if delta.get('playing') is False:
                attempt.play_requested=False
            elif delta.get('playing') is True and not attempt.play_requested:
                delta={key:value for key,value in delta.items() if key!='playing'}
        swapping=self._vst_swap_intent(delta)
        r = self._vst_active()
        if r is None and swapping:
            # Manual mount preflight is not yet the active renderer. Keep its
            # foreground silent transport responsive and only the exact same
            # Notes project's replacement intent in sync.
            if self._mem_goal is not None and any(key in delta for key in ('playing', 'position', 'loop')):
                binding = self._notes_transport_binding()
                if self._notes_warming_silent(self._vst_swap, binding):
                    return super()._output_change(**delta)
            self._notify_midi()
            return
        if r is None:
            if delta.get('audition') == 'joint' and self._mem_goal is not None:
                # The original audio keeps playing; no second audible MIDI owner.
                self._leave_memory()
            if delta.get('audition') == 'audio' and self._kept_device is not None and not self._kept_device.closing:
                self._kept_device.set_direct_mode(True)
            return super()._output_change(**delta)
        if delta.get('audition') == 'audio':
            if self._kept_device is not None and not self._kept_device.closing:
                self._vst_suspend_for_tracks()
            else:
                self._vst_leave(reason='audio_audition_requested')
            return super()._output_change(**delta)
        if delta.get('audition') in ('midi','joint'):
            joint = delta['audition'] == 'joint'
            source = self._vst_pcm_source() if joint else None
            r.set_pcm(source)
            self._audition_together = joint
            if self._project and self._project.get('editor'):
                self._project['editor']['audition_together'] = joint
        if 'playing' in delta and type(delta['playing']) is not bool:
            raise ValueError('无效的播放状态。')
        if 'loop' in delta:
            r.set_loop(*delta['loop'])
        # Pause before positioning on a combined paused-source request.
        if delta.get('playing') is False and r.wants_play:
            r.transport('pause')
        if 'position' in delta:
            r.transport('seek', delta['position'], track_notes_seek=not getattr(r, '_score_source', False) and (notes_page_seek or self._notes_transport_binding() is not None))
            if (attempt and attempt.swap is self._vst_swap and attempt.swap is not None
                    and attempt.swap['candidate'] is r and attempt.play_requested and
                    attempt.swap['playing'] and not r.ready and not r.wants_play):
                r.transport('play')
        if delta.get('playing') is True:
            if r.error:
                raise ValueError(r.error)
            if not r.wants_play:
                r.transport('play')
        self._vst_pump()

    def _set_together(self, enabled):
        raise ValueError('原曲与 MIDI 联合播放已暂时移除。')

    def _toggle_together(self):
        raise ValueError('原曲与 MIDI 联合播放已暂时移除。')

    def _vst_finish_together(self, r):
        pending = getattr(self, '_vst_together_pending', None)
        if not pending or pending[0] is not r:
            return
        _, prior, wanted = pending
        wire = r.wire.get('pcm') or {}
        matches = wire.get('epoch') == r.epoch and (
            all(wire.get(k) == wanted.get(k) for k in ('key','path','identity','offset_seconds'))
            if wanted is not None else wire.get('path') is None)
        error = r.pcm_error or (wire.get('error') if matches else None)
        if error:
            self._vst_together_pending = None
            r.set_pcm(prior)
            r.notice = str(error)
            self._output_command_error = str(error)
        elif r.pcm_ready:
            self._vst_together_pending = None
            if wanted is not None and not r.wants_play:
                r.transport('play')
            self._audition_together = wanted is not None
            if self._project and self._project.get('editor'):
                self._project['editor']['audition_together'] = self._audition_together
            self._publish()

    def _notes_resume_silent(self, binding, notes):
        if (self._vst is not None or self._mem_goal is not None or self._vst_retiring
                or not self._vst_matches(binding)):
            return
        active = self._notes_transport_binding()
        if active is None or active.owner is not binding.owner:
            return
        # Bypass saved-rack restoration. Retirement and the unmount write
        # have both completed; selecting the device-free clock cannot Play.
        super()._select_live_editor(binding.uri, resume=(notes[0], False))
        a, b, enabled = notes[1]
        self._audition_range = dict(start=a, end=b, loop=enabled)
        self._output_change(loop=notes[1])

    def _toggle_midi_output(self, audition):
        binding = self._notes_transport_binding()
        if binding is None:
            return self._toggle_midi_renderer(audition)
        if getattr(self, '_vst_pending_unmount', None) is not None:
            return  # Never grant a stale Play while the old renderer owns output.
        state = self._notes_session(binding)
        pending = self._vst_pending_transport()
        attempt = saved_restore.lookup(self)
        warming = self._notes_warming_silent(pending, binding)
        if warming:
            _, ended = self._memory_view()
            wanted = self._mem_goal.playing and not ended
        else:
            wanted = (pending['playing'] if pending is not None else
                      attempt.play_requested if attempt is not None else self._output_intent().playing)
        checkpoint = self._notes_checkpoint(binding)
        if wanted:
            from ..ui_preferences import saved_ui_preferences
            at = state.anchor if saved_ui_preferences(self._prefs)['notes_return_on_pause'] else None
            # A bare silent Pause bumps its locator and resends the stored Goal
            # position. Use the live (or unacknowledged explicit) locator instead.
            if at is None and self._vst is None and self._mem_goal is not None and checkpoint is not None:
                at = checkpoint[0]
            if at is not None and (self._vst is not None or self._mem_goal is not None or pending is not None):
                return self._output_change(audition='midi', playing=False, position=at)
        if not wanted:
            anchor = checkpoint[0] if checkpoint is not None else 0.
            duration = self._vst.duration if self._vst is not None else self._output_intent().midi_duration
            if duration > 0 and anchor >= duration:
                anchor = 0.  # Natural EOF follows the renderer's established replay-from-start rule.
            state.anchor = anchor
        if warming:
            # An actual click may Play; merely accepting the instrument never
            # inherits the silent engine's previously active Play request.
            return self._output_change(audition='midi', playing=not wanted)
        return self._toggle_midi_renderer(audition)

    def _toggle_midi_renderer(self, audition):
        if audition == 'joint':audition = 'midi'
        pending=self._vst_pending_transport()
        if pending is not None:
            playing=not pending['playing']
            attempt=pending.get('saved_reopen')
            if attempt is not None and attempt.swap is pending:
                attempt.play_requested=playing
            return self._output_change(audition=audition,playing=playing)
        if self._vst is None and audition == 'midi' and self._mem_goal is None and (self._midi_file or {}).get('path') == self._editor_uri:
            binding = self._notes_transport_binding()
            notes = self._notes_checkpoint(binding) if binding is not None else None
            self._select_live_editor(self._editor_uri, resume=(notes[0] if notes is not None else 0., False))
        if self._vst is None:
            if self._vst_current_fault():
                self._notify_midi()
                return
            if audition=='midi' and self._mem_goal is not None and (self._midi_file or {}).get('path')==self._editor_uri:
                try:
                    binding=self._vst_context()
                except (KeyError,ValueError,RuntimeError):
                    binding=None
                if binding is not None and saved_restore.toggle_notes_play(self,binding):
                    self._notify_midi()
                    return
            return super()._toggle_midi_output(audition)
        return self._output_change(audition=audition, playing=not self._vst.wants_play)

    def _update_notes_seek_anchor(self, binding, position):
        if binding is None or getattr(self, '_score_transport', None):
            return
        r = self._vst
        if r is not None and not getattr(r, '_score_source', False) and r.loop:
            start, end, enabled = r.loop
            if enabled and start < end and not (start <= position < end):
                # Out-of-loop seeks are rejected by native RT; keep the live anchor.
                return
        anchor = max(0., float(position))
        duration = r.duration if r is not None else self._output_intent().midi_duration
        if duration > 0:
            anchor = min(anchor, duration)
            if anchor >= duration:
                anchor = 0.
        self._notes_session(binding).anchor = anchor

    def _seek_midi(self, position):
        binding = self._notes_transport_binding()
        pending = self._vst_pending_transport()
        if pending is not None:
            if binding is not None and pending.get('playing'):
                self._update_notes_seek_anchor(binding, position)
            return self._output_change(position=position)
        if self._vst is None:
            if binding is not None and self._mem_goal is not None:
                _, ended = self._memory_view()
                if self._mem_goal.playing and not ended:
                    self._update_notes_seek_anchor(binding, position)
            return super()._seek_midi(position)
        if getattr(self, '_vst_page_suspended', False) and not getattr(self, '_score_transport', None):
            # Explicit Notes seek on a warm suspended VST must wake it rather than
            # falling through to Tracks audio and leaving the VST at its old cursor.
            return self._output_change(audition='midi', position=position, _notes_page_seek=True)
        if binding is not None and self._vst.wants_play:
            # A new locator must not implicitly cancel a pending explicit Play,
            # and becomes the new return-on-pause anchor for the active run.
            self._update_notes_seek_anchor(binding, position)
            return self._output_change(position=position, playing=True, _notes_page_seek=True)
        return self._output_change(position=position, _notes_page_seek=not getattr(self, '_score_transport', None))

    def _stop_midi(self):
        binding = self._notes_transport_binding()
        if binding is not None:
            self._notes_session(binding).anchor = None
        if self._vst_pending_transport() is not None:return self._output_change(playing=False,position=0.)
        if self._vst is None:
            attempt = saved_restore.lookup(self)
            if attempt is not None:
                attempt.play_requested = False
                self._notify_midi()
            return super()._stop_midi()
        self._vst.transport('stop')
        self._vst.clear()
        self._vst_pump()

    def _stop_audio(self):
        return super()._stop_audio() if self._vst_active() is None else self._stop_midi()

    def _sync_editor_transport(self, **kwargs):
        if self._vst is None:
            return super()._sync_editor_transport(**kwargs)
        r = self._audition_range or self._vst_binding.owner.head['range']
        self._vst.set_loop(r['start'], r['end'], r['loop'])
        return True

    def _queue_settings_asio_handoff(self, manager, route):
        endpoint = self._kept_device
        pending = getattr(self, '_settings_asio_handoff', None)
        previous_setup = pending['previous_setup'] if pending else manager.previous
        previous_route = pending['previous_route'] if pending else None
        if not pending and endpoint is not None and endpoint.info is not None:
            old_route = dict(endpoint.device, asio_candidates=(endpoint.info['device'],))
            if endpoint.reuse_open_asio(old_route, require_requested_frames=False) is not None:
                previous_route = old_route
        self._settings_asio_handoff = dict(requested=manager.current, route=route,
                                            previous_setup=previous_setup, previous_route=previous_route,
                                            started=None, error=None, rollback=False)
        # Stop the logical transport before releasing its physical callback.
        # A failed Handoff with no owner is stale UI state, not a held device.
        h = self._output
        if h is not None:
            h.sync_web_intent()
            position = h.intent.position
            if h.owner is not None:
                sample = h.owner.snapshot()
                target = getattr(sample, 'pending_target', None)
                if target is not None:
                    position = target
                elif getattr(sample, 'ready', False) and not getattr(sample, 'error', None):
                    position = sample.position
            self._output_desired = replace(h.intent, position=position, playing=False)
            self._device_resume_position = position
            self._output = None
            h.close()
            if h.owner is not None:
                self._output_retiring.append(h)
        self._deferred_delta = None
        self._failed_tracks_retry = None
        self._output_command_error = self._reported_output_error = None
        self._raw_failure_reason = self._native_shared_failure = None
        self._player_tick()

    def _pump_settings_asio_handoff(self):
        pending = getattr(self, '_settings_asio_handoff', None)
        if pending is None:
            return
        manager = self._dm()
        if self._output_shutdown or manager.current != pending['requested']:
            self._settings_asio_handoff = None
            return
        # Do not start or tear down the physical endpoint while a logical
        # driver consumer still owes an explicit release acknowledgement.
        if (self._output_retiring or self._vst_retiring or getattr(self, '_mem_retiring', None)
                or getattr(self, '_asio_panel', None) is not None
                or any(not ((s := owner.snapshot()).closed and s.output_released)
                       for owner in self._output_owners)):
            return
        endpoint = self._kept_device
        try:
            if endpoint is not None and endpoint is not pending['started'] and not endpoint.closing:
                endpoint.close()
            if endpoint is not None and endpoint.closing:
                endpoint.poll()
                if endpoint.handle:
                    return
                self._kept_device = endpoint = None
            if endpoint is None:
                endpoint = self._ensure_kept_endpoint(device=pending['route'], direct_mode=True)
                pending['started'] = endpoint
                manager.message = '正在打开音频驱动…'
                self._player_tick()
                return
            if endpoint is not pending['started']:
                # A newly attached external owner is not ours to replace.
                raise RuntimeError('输出所有者发生变化')
            endpoint.poll()
            actual = endpoint.reuse_open_asio(pending['route'], require_requested_frames=False)
            if actual is None:
                return
            requested = pending['requested']
            previous = pending['previous_setup'] if pending['rollback'] else None
            manager.confirm_kept_switch(requested, actual, previous=previous, error=pending['error'])
        except (OSError, ValueError, RuntimeError) as exc:
            if endpoint is not None and endpoint.closing:
                # Cannot prove release. Keep waiting; do not open a replacement.
                return
            if endpoint is not None and endpoint is pending['started']:
                endpoint.close()
            if not pending['rollback'] and pending['previous_route'] is not None and pending['previous_setup'] is not None:
                pending.update(route=pending['previous_route'], started=None, error=str(exc), rollback=True)
                manager.message = '所选驱动未能打开，正在恢复原输出…'
                self._player_tick()
                return
            self._settings_asio_handoff = None
            self._deferred_delta = None
            manager.failed(exc)
            self._player_tick()
            return
        self._settings_asio_handoff = None
        self._output_ui_signature = None
        self._player_tick()
        self._shared_prepare()

    def _switch_output_device(self, *, refresh_catalog=True):
        manager = self._dm()
        manual_route = getattr(manager, 'manual_asio_route', None)
        in_progress = getattr(self, '_settings_asio_handoff', None) is not None
        if manual_route is not None and self._vst is None and (
                getattr(self, '_native_tracks', False) or
                (self._output is None and (in_progress or not self._sources))):
            return self._queue_settings_asio_handoff(manager, manual_route)
        if in_progress:
            self._settings_asio_handoff = None
        if refresh_catalog and manual_route is None:
            self._vst_refresh_open_catalog()
        if self._vst_swap is not None and self._vst_swap.get('background_suspend'):
            saved_restore.abandon(self, include_background=True)
            self._vst_cancel_swap()
        if self._output is not None and self._output_shares_kept_endpoint():
            self._output.close()
            self._output_retiring.append(self._output)
            self._output = None
        choice = self._vst_choice
        if not choice or not self._vst_matches(choice[0]):
            if self._kept_device is not None and self._vst is None:
                self._kept_device.close()
            return super()._switch_output_device()
        try:
            route = manual_route or global_device_route(manager.current, None, active_endpoint=None)
        except (ValueError,OSError,RuntimeError) as exc:
            self._vst_state_warning=str(exc)
            if self._vst is not None:self._vst.transport('pause')
            self._notify_midi()
            raise
        binding,instrument,libraries,_,restore = choice
        if self._vst is not None:
            self._vst_choose(binding,instrument,libraries,route,True)
        else:
            if self._kept_device is not None:
                self._kept_device.close()
            self._vst_choice = (binding,instrument,libraries,route,restore)
            super()._switch_output_device()

    def _set_audition_rate(self, value):
        if self._output_shutdown or getattr(self._vst,'_score_source',False):
            return
        rate = audition_rate_value(value)
        pending = self._vst_pending_transport()
        if self._vst is None and pending is None:
            return super()._set_audition_rate(rate)
        if rate != 1. and ((self._vst is not None and self._vst.pcm_source is not None)
                          or (pending and pending.get('pcm') is not None)):
            raise ValueError('原曲与 MIDI 对比只支持原速。')
        if rate != getattr(self, '_audition_rate', 1.):
            self._vst_swap_intent(dict(rate=rate))
        # Audition speed is session transport state, not an edit to the
        # project tempo map. Never activate an audio owner to change it.
        if self._vst is not None:
            self._vst.set_rate(rate)
        self._audition_rate = rate
        self._output_desired = replace(self._output_desired, rate=rate)

    def _vst_instrument_gain(self):
        gain = getattr(self, '_vst_midi_gain', None)
        if gain is not None:
            return gain
        volume = getattr(self, '_midi_volume', None)
        # Keep the old audible default; the footer's 65% was only a UI placeholder.
        return 1. if volume is None else max(0., min(100., float(volume))) / 100

    def _set_vst_instrument_gain(self, message):
        if self._closed or self._output_shutdown or getattr(self, '_editor_closing', False):
            raise ValueError('窗口正在关闭。')
        from math import isfinite
        scope = json.loads(message)
        if not isinstance(scope, dict):
            raise ValueError('乐器推子请求无效。')
        binding = self._vst_context()
        if scope.get('project') != binding.project['path'] or type(scope.get('generation')) is not int or scope['generation'] != self._edit_generation_number:
            raise ValueError('工程已切换；未调整乐器音量。')
        current = self._vst_mount_value()
        if not current['mounted'] or current['key'] != scope.get('key'):
            raise ValueError('当前乐器已切换；未调整音量。')
        gain = scope.get('gain')
        if type(gain) not in (int,float) or not 0. <= gain <= VST_MIDI_GAIN_MAX or not isfinite(gain):
            raise ValueError('乐器增益超出允许范围（静音至 +6 dB）。')
        before = self._vst_instrument_gain()
        self._vst_midi_gain = float(gain)
        if gain != before:
            candidate = (self._vst_swap or {}).get('candidate')
            if candidate is not None and candidate is not self._vst:
                candidate.set_mix(self._vst_mix())  # Gain only; do not admit a route here.
            self._vst_sync_mix()
            self._notify_midi()
        return self._vst_instrument_gain()

    def _vst_mix(self):
        master = max(0., min(100., float(self._volume))) / 100
        scope=getattr(self,'_score_transport',None)
        if getattr(self._vst,'_score_source',False):
            click=bool(scope and scope['route'] is self._vst and scope['click_available'] and scope['metronome'])
        else:
            click=bool(getattr(self,'_editor_metronome',False))
        return dict(master_gain=master, midi_gain=self._vst_instrument_gain(), audio_gain=1., click_gain=1.,
                    muted=bool(self._muted), metronome=click)

    def _vst_sync_mix(self):
        if self._vst is not None:
            mix = self._vst_mix()
            self._vst.set_mix(mix)
            self._vst_pump()

    def _set_volume(self, value):
        super()._set_volume(value)
        self._vst_sync_mix()

    def _set_midi_volume(self, value):
        if self._vst is None:
            return super()._set_midi_volume(value)
        from math import isfinite
        if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 100:
            raise ValueError('MIDI volume must be between 0 and 100')
        self._midi_volume = value
        self._vst_midi_gain = value / 100
        self._vst_sync_mix()

    def _set_metronome(self, enabled):
        if self._vst is None:
            return super()._set_metronome(enabled)
        self._editor_metronome = bool(enabled)
        if self._project and self._project.get('editor'):
            self._project['editor']['metronome'] = self._editor_metronome
        self._vst_sync_mix()  # No legacy click owner or second device.
        if hasattr(self,'_current_review_click') and self._current_review_click() is not None:
            self._sync_review_click()

    def _midi_state(self, **kwargs):
        r = self._vst
        if r is None:
            state = dict(super()._midi_state(**kwargs), vst_epoch=None, vst_feedback=False,
                         vst_instrument=self._vst_mount_state(), vst_midi_gain=self._vst_instrument_gain())
            fault = self._vst_current_fault()
            if fault and (self._midi_file or {}).get('path') == fault['binding'].uri:
                state.update(position=fault['position'], duration=fault['duration'], playing=False,
                             available=False, preparing=False, play_pending=False, loading=False,
                             error=fault['message'], message=fault['message'])
            attempt = saved_restore.current(self)
            if attempt and attempt.pending and (self._midi_file or {}).get('path') == attempt.binding.uri:
                wanted = attempt.swap['playing'] if attempt.swap is not None else attempt.play_requested
                state.update(playing=False, available=True, preparing=True, play_pending=wanted,
                             message='正在连接已保存音源…')
            return state
        error = r.error or r.projection_error or r.rate_error or r.notice or r.wire.get('last_error')
        score_scope=getattr(self,'_score_transport',None)
        score_source=bool(score_scope and score_scope['route'] is r)
        pending = r.wants_play and not r.playing and not r.retiring
        if score_source:
            pending = pending or bool(score_scope['desired'] and score_scope['desired']['play'])
        score_seek_ready=bool(score_source and score_scope['revision'] is not None
                             and r.applied is not None and r.applied==r.wanted and not r.projection_error)
        score_controls=None
        if score_seek_ready and score_scope['bar_authority']:
            from fractions import Fraction
            authority=score_scope['bar_authority']
            bar=authority.follow(authority.session,score_scope['revision'],r.applied,Fraction(str(r.position)))
            if bar is None and r.position>=r.duration:
                bar=authority.bars[-1]
            if bar:
                start,end,wanted_loop=score_scope['score_range'] or (float(bar.start),float(bar.end),False)
                loop=wanted_loop and bool((r.wire.get('transport') or {}).get('loop_enabled'))
                score_controls=dict(start=start,end=end,loop=loop,duration=r.duration,position=r.position,revision=score_scope['revision'],
                                    flatTempo=self._score_flat(),
                                    ordinal=bar.source_ordinal,occurrence=bar.occurrence,page=bar.page,y=bar.y,
                                    measure=bar.source_ordinal+1, speed=r.playback_rate,
                                    requestedSpeed=r.requested_rate, speedMin=.1 if r.score_rate_supported else .25,
                                    speedMax=3. if r.score_rate_supported else 2.,
                                    capabilities=['playPause','pause','stop','rewind','seek','speed','flatTempo'])
                if score_scope['count_geometry']:
                    score_controls['capabilities'].append('countIn')
                    score_controls['countIn']=score_scope['count_in']
                    score_controls['countingIn']=r.counting
                if score_scope['click_available']:
                    score_controls['capabilities'].append('metronome')
                    score_controls['metronome']=bool((r.wire.get('mix') or {}).get('metronome'))
                    score_controls['requestedMetronome']=score_scope['metronome']
                if score_scope['written_bounds']:
                    score_controls['capabilities']+=['loop','loopIn','loopOut']
                    if score_scope['score_markers']:
                        score_controls['loopInTick'],score_controls['loopOutTick']=score_scope['score_markers']
                ticks=score_scope['ticks']
                if ticks:
                    score_controls['capabilities']+=['seekSelection','seekTick']
                    current=ticks.written_at(Fraction(str(r.position)))
                    try:
                        from bisect import bisect_right
                        played=ticks.tempo[max(0,bisect_right(ticks._tempo_times,Fraction(str(r.position)))-1)]
                        score_controls['tempoBpm']=60_000_000/played.microseconds_per_quarter
                        score_controls['tempoUnit']='quarter'
                    except (AttributeError,IndexError,TypeError,ZeroDivisionError):
                        pass
                    if current is not None:
                        score_controls['writtenTick'],score_controls['tickOccurrence']=current
                        beat_map=score_scope['beat_map']
                        measured=beat_map.at(current[0]) if beat_map else None
                        if measured:
                            score_controls['measure'],score_controls['beat'],score_controls['maxBeat']=measured
                            score_controls['maxMeasure']=beat_map.maximum
                            score_controls['capabilities'].append('seekBeat')
        return dict(score_source=score_source,score_seek_ready=score_seek_ready,score_controls=score_controls,duration=r.duration,
                    position=r.requested_position if not score_source else r.position,
                    rendered_position=r.position,
                    sampled_qpc_sec=r.sampled_qpc_sec,
                    seek_active=bool(getattr(r,'_requested_seek',None) is not None or r.motion is not None or (r.wanted is not None and r.applied!=r.wanted)),
                    rate_pending=bool(r.rate_request is not None),
                    loop_enabled=bool((r.wire.get('transport') or {}).get('loop_enabled')),
                    playing=(r.playing and r.wants_play) if not score_source else r.playing, rate=r.playback_rate,
                    transport_order=perf_counter(), sampled_at=None, loading=False,
                    available=not r.retiring and (not r.projection_error or r.playing),
                    preparing=pending, play_pending=pending, message=error or '',
                    error=error, vst_instrument=self._vst_mount_state(), vst_midi_gain=self._vst_instrument_gain(), vst_epoch=r.epoch, vst_feedback=self._vst_feedback and r.ready and r.applied is not None,
                    playback_warnings=list(r.projection_warnings), position_kind='rendered_project', vst_mix_inputs=self._vst_mix(), vst_mix_actual=r.wire.get('mix'))

    def _vst_driver_readback(self, route):
        endpoint=getattr(self,'_kept_device',None)
        if (route is None or route.retiring or route.error or endpoint is None
                or route.device.get('backend')!='asio' or not endpoint.matches(route.device)
                or getattr(self,'_asio_panel',None) or getattr(self,'_vst_panel_transition',None)):
            return None
        read=getattr(endpoint,'driver_parameters',None)
        return read() if read else None

    def _player_view(self, include_sources, s, *, compact=False):
        view = super()._player_view(include_sources, s, compact=compact)
        view['audition_rate'] = getattr(self, '_audition_rate', 1.)
        r = self._vst_active()
        if r is not None:
            d = r.wire.get('device') if r.ready else None
            error = (r.error or r.projection_error or r.pcm_error or (r.wire.get('pcm') or {}).get('error')
                     or r.rate_error or r.notice or r.wire.get('last_error') or getattr(self, '_output_command_error', None))
            status = self._output_status_base(None)
            status.update(audition='joint' if r.pcm_source is not None else 'midi',
                          displayMode='joint' if r.pcm_source is not None and r.pcm_ready else 'midi', midiEngine='vst',
                          actual='vst' if r.ready else None,
                          phase='playing' if r.playing else 'error' if r.error else 'paused' if r.ready else 'idle',
                          label='VST · '+r.instrument.name, diagnostic=error, requiresResume=False,
                          device=dict(backend=r.device['backend'], device=r.device['device_id'],
                                      requested=dict(r.device), actualParameters=d,
                                      driverParameters=self._vst_driver_readback(r),
                                      actual=(f'{d["sample_rate"]} Hz · {d["buffer_frames"]} 帧' if d else None)),
                          vst=dict(epoch=r.epoch, realtime_ready=r.ready, submitted_sequence=r.submitted,
                                   applied_sequence=r.applied, playback_warnings=list(r.projection_warnings), position_kind='rendered_project',
                                   plugin_latency_samples=r.wire.get('plugin_latency_samples'),
                                   output_latency_samples=r.wire.get('output_latency_samples'),
                                   underruns=r.wire.get('underruns'), pcm=r.wire.get('pcm'),
                                   pcm_error=r.pcm_error))
            view.update(audition='joint' if r.pcm_source is not None and r.pcm_ready else 'midi',
                        playing=False, position=r.position, rate=r.playback_rate, sampled_at=None,
                        preparing=r.pcm_source is not None and not r.pcm_ready, outputStatus=status, output_status_revision=-1)
        elif self._vst_retiring and self._output is None:
            status = self._output_status_base(None)
            status.update(phase='blocked', label='VST 输出尚未释放', diagnostic=self._vst_retirement_message())
            view.update(playing=False, outputStatus=status, output_status_revision=-1)
        elif (fault := self._vst_current_fault()) and (self._midi_file or {}).get('path') == fault['binding'].uri:
            status = self._output_status_base(None)
            status.update(phase='error', audition='midi', midiEngine='vst',
                          label=f"{fault['instrument'].name} 已断开", diagnostic=fault['message'])
            view.update(playing=False, outputStatus=status, output_status_revision=-1)
        elif (swap:=getattr(self,'_vst_swap',None)) is not None:
            route=swap.get('candidate')
            if (info:=self._vst_driver_readback(route)) is not None:
                # The physical driver and the waiting instrument are separate.
                # This is only device metadata, not transport readiness.
                status=dict(view.get('outputStatus') or self._output_status_base(None))
                status['device']=dict(backend='asio',device=route.device.get('device_id'),driverParameters=info)
                view['outputStatus']=status
        return view

    def _edit_prepare_close(self, window=None):
        approved = getattr(self, '_vst_close_approval', None)
        if (approved is not None and self._editor_close_release is not None and
                approved == self._close_identity(live_owner.owners()) and
                not any(e.status()['busy'] for e in live_owner.owners()) and
                not self._edit_has_pending_vst_work(live_owner.owners())):
            return True
        self._vst_close_approval = None
        return super()._edit_prepare_close(window)

    def close(self):
        parent = self.parent()
        if not self._edit_prepare_close(parent.window() if isinstance(parent,QWidget) else None):
            return False
        self._vst_close_approval = self._close_identity(live_owner.owners())
        saved_restore.abandon(self)
        self._vst_cancel_swap()
        self._nav_parking_discard()
        if self._output is not None:
            self._output.close()
            self._output_retiring.append(self._output)
            self._output = None
        self._vst_leave(capture=False, reason='application_closing')  # The Save/Discard decision owns persistence.
        self._vst_choice = None
        for old in self._vst_retiring:
            old.retire()
        self._vst_pump()
        if self._kept_device is not None and not self._vst_retiring and not self._output_retiring:
            self._kept_device.close()
            try:self._kept_device.poll()
            except Exception:pass
            if not self._kept_device.handle:
                self._kept_device = None
        if self._vst_retiring and not getattr(self, '_output_close_manages_vst', False):
            if not getattr(self, '_vst_close_notice', False):
                self._vst_close_notice = True
                self.logs.emit('Closing VST output; close again after device release is confirmed.')
            return False
        if self._instrument_standby is not None:
            self._instrument_standby.close()
        return super().close()
