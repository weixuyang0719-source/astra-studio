"""GUI publication of native receipts; checkpoint completion is status only."""
from copy import deepcopy
from pathlib import Path
import re
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, QEvent, QTimer, Slot
from PySide6.QtWidgets import QWidget

from . import editing, document_editor as nd, live_owner as live
from .save_dialogs import CloseBusyDialog, CloseConfirmDialog, CloseSaveErrorDialog
from .studio_editing import range_grid_view

ACTIONS = frozenset(('editProject','undoProject','redoProject','setEditorRange'))
DURING_EDIT = frozenset()  # No GUI edit queue survives a dispatch turn.
DEFAULT_AUTOSAVE = dict(enabled=True, interval_minutes=5, retention_count=10)
_NOTE_FIELDS = ('revision','edit_stamp','operation','manual','can_undo','can_redo',
                'dirty','score_stale','exported_revision','preview_pending','metronome','audition_together')


def autosave_settings(prefs, patch=None):
    def valid(key, value):
        if key == 'enabled':
            return type(value) is bool
        return type(value) is int and 1 <= value <= (120 if key == 'interval_minutes' else 100)

    result = dict(DEFAULT_AUTOSAVE)
    stored = prefs.get('editor_autosave')
    if isinstance(stored, dict):
        result.update({key: stored[key] for key in result if key in stored and valid(key, stored[key])})
    if patch is not None:
        if not isinstance(patch, dict) or patch.keys() - result.keys():
            raise ValueError('无效的自动保存设置。')
        if any(not valid(key, value) for key, value in patch.items()):
            raise ValueError('请检查自动保存开关、间隔（1–120 分钟）和副本数量（1–100 份）。')
        result.update(patch)
    return result


def _CloseWait(window, entries, *, check_done=None, title='正在保存',
               heading='正在完成已经开始的保存…', lead='正在将工程更改安全写入磁盘，请稍候。',
               delay_ms=160):
    """GUI stays responsive; cancelling never cancels/replays a disk write."""
    predicate = check_done or (lambda: not any(e.status()['busy'] for e in entries))
    return CloseBusyDialog(window, predicate, title=title, heading=heading, lead=lead, delay_ms=delay_ms)


_GESTURE_ACTIONS = ('note_create', 'note_drag', 'cc_insert', 'cc_sweep', 'cc_drag')
_PULSE_PROVIDER = 'cubase_local_reconstruction'


def _gesture_detection(payload, doc, key):
    """Resolve the identifier against the host's current detector projection."""
    if not isinstance(payload, dict) or payload.get('available') is False or payload.get('cache_key') != key:
        raise ValueError('原始检测拍证据已改变；本次手势未应用。')
    tempo = doc['tempo']
    evidence = tempo.get('pulse_evidence') or {}
    candidate = tempo.get('adopted_candidate') or {}
    native = (tempo.get('source') == _PULSE_PROVIDER or bool(tempo.get('pulse_source'))
              or isinstance(candidate, dict) and candidate.get('provider') == _PULSE_PROVIDER
              or isinstance(evidence, dict) and evidence.get('provider') == _PULSE_PROVIDER)
    if native:
        ref = payload.get('evidence_ref')
        digests = ('project_source_sha256', 'normalized_sha256', 'result_sha256', 'observations_sha256', 'decode_key')
        fields = ('version', 'provider', 'result_path', *digests)
        valid = (payload.get('source') == 'cubase_local_cache' and payload.get('available') is True
                 and payload.get('provider') == _PULSE_PROVIDER and isinstance(ref, dict) and isinstance(evidence, dict)
                 and type(ref.get('version')) is int and ref['version'] == 1 and ref.get('provider') == _PULSE_PROVIDER
                 and all(k in ref and k in evidence and ref[k] == evidence[k] for k in fields)
                 and all(isinstance(ref[k], str) and re.fullmatch('[a-f0-9]{64}', ref[k]) for k in digests)
                 and payload.get('result_sha256') == ref['result_sha256'] and key == ref['decode_key'])
    else:
        valid = payload.get('source') == 'beat_this_cache'
    rows = payload.get('seconds')
    if not valid or not isinstance(rows, list) or not rows or not all(
            type(t) in (int, float) and 0 <= t <= (doc['duration'] or 1.) + .001 for t in rows):
        raise ValueError('当前检测拍证据不可用；本次手势未应用。')
    return rows


class EditTransactions:
    def _edit_init(self):
        if hasattr(self, '_save_timer'):
            return
        self._edit_receipts = {}
        self._autosave_at = monotonic()
        self._shown_save_status = None
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self._edit_save_tick)
        self._save_timer.start()
    def _edit_identity(self):
        project = self._project
        path = str((project or {}).get('path',''))
        if not hasattr(self,'_edit_generation') or self._edit_project_ref is not project or self._edit_project_path != path:
            prior = getattr(self, '_edit_project_path', '')
            self._notes_score_retire()
            self._edit_project_ref, self._edit_project_path = project, path
            self._edit_generation = object()
            self._edit_generation_number = getattr(self,'_edit_generation_number',0) + 1
            self._editor_transaction = None
            self._editor_native_stamp = None
            self._editor_uri = 'astra-editor:' + uuid4().hex
            self._shown_save_status = None
            self._cancel_midi_intent()
            if prior and prior != path:
                self._edit_clear_beat_recording_for_close()
                live.release(prior)  # dirty or saving owners stay in the registry
        return self._edit_generation

    def _edit_stamp(self, doc, request=None):
        self._edit_identity()
        self._edit_publication_number = getattr(self,'_edit_publication_number',0) + 1
        doc['edit_stamp'] = dict(generation=self._edit_generation_number,
                                 version=self._edit_publication_number,request=request)

    def _edit_pending(self):
        return False

    def _edit_revision_pending(self):
        return False

    def _edit_preview_invalidate(self):
        pass  # No file-preview tickets are created in this lane.

    def _edit_preview_selected(self, doc=None):
        if doc is None:
            doc = (self._project or {}).get('editor') or {}
        path = doc.get('preview_path')
        return bool(path and path == (self._midi_file or {}).get('path'))

    def _edit_midi_status(self):
        selected = self._edit_preview_selected()
        return False, ((self._project['editor'].get('preview_error') or None) if selected else None)

    def _edit_project_label(self, project_path):
        target = live.key(project_path)
        current = self._project or {}
        if current.get('path') and live.key(current['path']) == target and current.get('name'):
            return str(current['name'])
        for row in getattr(self, '_recent', ()) or ():
            if row.get('path') and live.key(row['path']) == target and row.get('name'):
                return str(row['name'])
        folder = Path(project_path)
        try:
            from .pipeline import read_json
            meta = read_json(folder / 'project.json')
            if isinstance(meta, dict):
                source = meta.get('source_audio') or meta.get('source')
                if isinstance(source, str) and source.strip():
                    stem = Path(source).stem
                    if stem:
                        return stem
        except (OSError, ValueError, TypeError):
            pass
        return folder.name or str(project_path)

    def _edit_dirty_summary(self, entry):
        status = entry.status()
        current = (self._project or {}).get('path')
        is_current = bool(current and live.key(current) == entry.project)
        musical = bool(status.get('has_musical_changes'))
        instrument = bool(status.get('has_instrument_changes'))
        if musical and instrument:
            detail = '音符 / 节奏编辑与乐器配置均尚未保存'
        elif instrument:
            detail = '乐器与效果链配置尚未保存'
        elif musical:
            detail = '音符 / 节奏编辑尚未保存'
        else:
            detail = '编辑尚未正式保存'
        return dict(
            name=self._edit_project_label(entry.project),
            path=entry.project,
            is_current=is_current,
            detail=detail,
            error=status.get('error'),
            has_musical=musical,
            has_instrument=instrument,
        )

    def _edit_save_status(self):
        project = (self._project or {}).get('path')
        current_key = live.key(project) if project else None
        entry = live.retained(project) if project else None
        ready = bool(entry and (self._project or {}).get('editor'))
        other_dirty = []
        for owner in live.owners():
            if owner.project == current_key:
                continue
            try:
                if owner.status().get('save_dirty'):
                    other_dirty.append(dict(path=owner.project, name=self._edit_project_label(owner.project)))
            except (OSError, ValueError, RuntimeError):
                continue
        state = dict(entry.status() if ready else {}, project=project,
                     project_name=(self._project or {}).get('name') if project else None,
                     ready=ready, generation=self._edit_generation_number,
                     other_dirty_projects=other_dirty)
        if state != getattr(self, '_editor_save_wire_state', None):
            self._editor_save_wire_state = state
            self._editor_save_wire_version = getattr(self, '_editor_save_wire_version', 0) + 1
        return dict(state, update=self._editor_save_wire_version)

    def _edit_emit_save_status(self):
        if getattr(self, '_closed', False):
            return
        status = self._edit_save_status()
        if status != self._shown_save_status:
            self._shown_save_status = status
            self.editorSaveChanged.emit(self._json(status))

    def _edit_autosave_changed(self, config):
        self._autosave_at = monotonic()
        for entry in live.owners():
            entry.saves.configure(config['enabled'], config['retention_count'])
        self._edit_emit_save_status()

    def _edit_beat_save_blocked(self, project=None):
        locked = getattr(self, '_beat_recording_save_project', None)
        return bool(locked and (project is None or locked == live.key(project)))

    @Slot()
    def _edit_save_tick(self):
        if self._closed or getattr(self, '_editor_closing', False):
            return
        self._edit_identity()
        config = autosave_settings(self._prefs)
        now = monotonic()
        due = config['enabled'] and now - self._autosave_at >= config['interval_minutes'] * 60
        if due:
            self._autosave_at = now
        current = (self._project or {}).get('path')
        notices = {}
        prior = getattr(self, '_editor_save_notices', {})
        for entry in live.owners():
            entry.saves.configure(config['enabled'], config['retention_count'])
            if due and not self._edit_beat_save_blocked(entry.project):
                try:
                    self._edit_enqueue_save(entry, kind='recovery')
                except (OSError, ValueError, RuntimeError) as exc:
                    entry.saves.capture_error('recovery', exc)
            status = entry.status()
            for name, message in (('formal', status['formal']['error']),
                                  ('recovery', status['recovery']['error']),
                                  ('retention', status['recovery']['warning'])):
                if message:
                    key = (entry.project, status['epoch'], name)
                    notices[key] = message
                    if prior.get(key) != message:
                        self.logs.emit(f'{entry.project}：{message}')
            if not current or entry.project != live.key(current):
                live.release(entry.project)
        self._editor_save_notices = notices
        self._edit_emit_save_status()
        self._notes_score_save_check()

    def _edit_enqueue_save(self, entry, *, kind='formal', retry=False):
        if self._edit_beat_save_blocked(entry.project):
            return dict(status='skipped', error='请先确认或取消录拍。')
        return entry.backup(retry=retry) if kind == 'recovery' else entry.save(retry=retry)

    def _edit_prepare_save_inspection(self, entries):
        pass

    def _edit_has_pending_vst_work(self, entries):
        return False

    def _edit_clear_beat_recording_for_close(self):
        prev = (
            bool(getattr(self, '_beat_recording_active', False)),
            bool(getattr(self, '_beat_recording_seek_locked', False)),
            getattr(self, '_beat_recording_save_project', None),
        )
        self._beat_recording_active = False
        self._beat_recording_seek_locked = False
        self._beat_recording_save_project = None
        vst = getattr(self, '_vst', None)
        if vst is not None:
            vst.recording_active = False
        return prev

    def _edit_restore_beat_recording(self, prev):
        if not prev:
            return
        active, seek_locked, project = prev
        self._beat_recording_active = active
        self._beat_recording_seek_locked = seek_locked
        self._beat_recording_save_project = project
        vst = getattr(self, '_vst', None)
        if vst is not None:
            vst.recording_active = active

    def _edit_save_all_dirty(self):
        try:
            if self._closed or getattr(self, '_editor_closing', False):
                raise ValueError('窗口正在关闭。')
            self._edit_identity()
            queued, skipped, errors = 0, 0, []
            for entry in live.owners():
                if not entry.status().get('save_dirty'):
                    continue
                if self._edit_beat_save_blocked(entry.project):
                    skipped += 1
                    errors.append(f'{self._edit_project_label(entry.project)}：请先确认或取消录拍。')
                    continue
                try:
                    reply = self._edit_enqueue_save(entry, kind='formal', retry=bool(entry.status()['formal']['failed_save']))
                    if reply.get('status') in ('failed', 'disabled', 'skipped'):
                        skipped += 1
                        if reply.get('error'):
                            errors.append(f'{self._edit_project_label(entry.project)}：{reply["error"]}')
                    else:
                        queued += 1
                except (OSError, ValueError, RuntimeError) as exc:
                    entry.saves.capture_error('formal', exc)
                    skipped += 1
                    errors.append(f'{self._edit_project_label(entry.project)}：{exc}')
            self._edit_emit_save_status()
            return dict(ok=not errors, queued=queued, skipped=skipped,
                        error='；'.join(errors) if errors else None,
                        save_status=self._edit_save_status())
        except (OSError, ValueError, RuntimeError) as exc:
            self.logs.emit(str(exc))
            return dict(ok=False, error=str(exc), save_status=self._edit_save_status())

    def _edit_manual_save(self, *, retry=False, kind='formal', project=None, generation=None):
        entry = None
        try:
            if self._closed or getattr(self, '_editor_closing', False):
                raise ValueError('窗口正在关闭。')
            self._edit_identity()
            current = (self._project or {}).get('path')
            if project is not None and (project != current or generation != self._edit_generation_number):
                raise ValueError('项目已切换，这次没有保存。')
            if current and self._edit_beat_save_blocked(current):
                return dict(ok=False, error='请先确认或取消录拍。')
            if not current or not (self._project or {}).get('editor'):
                raise ValueError('请先打开可编辑项目。')
            entry = live.retained(current)
            if entry is None:
                raise ValueError('项目会话已变化，请重新打开。')
            if kind == 'recovery' and retry:
                reply = self._edit_enqueue_save(entry, kind='recovery', retry=True)
            elif kind == 'retention' and retry:
                reply = entry.saves.enqueue(kind='retention', retry=True)
            elif kind == 'formal':
                reply = self._edit_enqueue_save(entry, retry=retry)
            else:
                raise ValueError('无效的保存请求。')
            self._edit_emit_save_status()
            return dict(ok=reply['status'] not in ('failed', 'disabled', 'skipped'),
                        save_status=self._edit_save_status(), **reply)
        except (OSError, ValueError, RuntimeError) as exc:
            if entry is not None and kind == 'formal' and not retry:
                entry.saves.capture_error('formal', exc)
                self._edit_emit_save_status()
            self.logs.emit(str(exc))
            return dict(ok=False, error=str(exc))

    @staticmethod
    def _close_identity(entries):
        return tuple((id(e), e.project, tuple(e.head['stamp']), e.status().get('instrument_generation'), e.status().get('instrument_route_revision'), e.status().get('instrument_capture_revision')) for e in entries)

    def _edit_is_idle(self, entries):
        # The close waiter also observes confirmed saves; the regular timer
        # deliberately does not run while the close prompt owns input.
        self._notes_score_save_check(closing=True)
        if self._notes_score_save_in_progress() or self._edit_has_pending_vst_work(entries):
            return False
        return not any(e.status()['busy'] for e in entries)

    def _edit_wait_saves(self, window, entries, *, title='正在保存',
                         heading='正在完成保存…', lead='正在将工程更改安全写入磁盘，请稍候。',
                         delay_ms=160):
        if self._edit_is_idle(entries):
            return True
        if window is None:
            return False
        return _CloseWait(window, entries, check_done=lambda: self._edit_is_idle(entries),
                          title=title, heading=heading, lead=lead, delay_ms=delay_ms).run()

    def _edit_prepare_close(self, window=None):
        # Runs once per close attempt. Never destroys drafts or evicts dirty
        # owners before every check and user decision is confirmed.
        self._editor_close_release = None
        if getattr(self, '_editor_closing', False):
            return False
        has_beat_recording = self._edit_beat_save_blocked()
        structure = getattr(self, '_structure_session', None)
        project_path = (self._project or {}).get('path')
        has_structure_draft = bool(project_path and structure and structure.pending(project_path))
        self._editor_closing = True
        saved_beat_state = None
        try:
            entries = live.owners()
            self._edit_prepare_save_inspection(entries)
            if not self._edit_wait_saves(window, entries,
                                         title='正在检查工程',
                                         heading='正在检查未保存的更改…',
                                         lead='正在确认项目与乐器状态，请稍候。'):
                return False
            entries = live.owners()
            identity = self._close_identity(entries)
            dirty = [e for e in entries if e.status()['save_dirty']]
            clean_with_recovery = [
                e for e in entries
                if not e.status()['save_dirty'] and e.status().get('recovery_touched') and e.status().get('sequence_ahead')
            ]
            discard_entries = ()
            clear_structure = False
            if dirty or has_structure_draft or has_beat_recording:
                if window is None:
                    return False
                items = [self._edit_dirty_summary(e) for e in dirty]
                dlg = CloseConfirmDialog(
                    window,
                    dirty_items=items,
                    has_structure_draft=has_structure_draft,
                    has_beat_recording=has_beat_recording,
                )
                dlg.exec()
                choice = dlg.choice
                if choice == 'cancel':
                    return False
                if choice == 'discard':
                    if has_beat_recording:
                        saved_beat_state = self._edit_clear_beat_recording_for_close()
                    discard_entries = tuple(dirty)
                    clear_structure = has_structure_draft
                elif choice in ('save', 'discard_recording_and_save'):
                    if choice == 'discard_recording_and_save':
                        saved_beat_state = self._edit_clear_beat_recording_for_close()
                    to_save = list(dirty)
                    retry_mode = False
                    while True:
                        save_errors = {}
                        for entry in to_save:
                            try:
                                use_retry = retry_mode and bool(entry.status()['formal'].get('failed_save'))
                                reply = self._edit_enqueue_save(entry, kind='formal', retry=use_retry)
                                if reply.get('status') in ('failed', 'disabled', 'skipped'):
                                    save_errors[entry] = reply.get('error') or '未能开始保存'
                            except (OSError, ValueError, RuntimeError) as exc:
                                entry.saves.capture_error('formal', exc)
                                save_errors[entry] = str(exc) or type(exc).__name__
                        if not self._edit_wait_saves(window, entries,
                                                     title='正在保存',
                                                     heading='正在保存项目…',
                                                     lead='正在将修改写入工程文件，请稍候。'):
                            self._edit_restore_beat_recording(saved_beat_state)
                            return False
                        current_owners = live.owners()
                        if {id(e) for e in current_owners} != {id(e) for e in entries}:
                            self.logs.emit('工程会话已变化，请重新关闭以确认。')
                            self._edit_restore_beat_recording(saved_beat_state)
                            return False
                        failed = [e for e in entries if e.status()['save_dirty'] or e in save_errors]
                        if not failed:
                            # Saving (including VST state capture or legacy migration)
                            # legitimately advances the saved head/capture revision.
                            identity = self._close_identity(entries)
                            clear_structure = has_structure_draft
                            break
                        err_items = [
                            dict(self._edit_dirty_summary(e),
                                 error=save_errors.get(e) or e.status().get('error') or '仍有未保存的编辑')
                            for e in failed
                        ]
                        err_dlg = CloseSaveErrorDialog(window, err_items)
                        err_dlg.exec()
                        if err_dlg.choice == 'retry':
                            to_save = failed
                            retry_mode = True
                            continue
                        if err_dlg.choice == 'discard':
                            identity = self._close_identity(entries)
                            discard_entries = tuple(failed)
                            clear_structure = has_structure_draft
                            break
                        self._edit_restore_beat_recording(saved_beat_state)
                        return False
                else:
                    return False
            # Nested Qt event loops may allow a project/owner change. Never
            # apply a decision to anything other than the exact inspected head.
            if identity != self._close_identity(live.owners()):
                self.logs.emit('编辑内容已变化，请重新关闭以确认。')
                self._edit_restore_beat_recording(saved_beat_state)
                return False
            if not self._edit_is_idle(entries):
                self._edit_restore_beat_recording(saved_beat_state)
                return False
            all_discard = tuple(dict.fromkeys((*discard_entries, *clean_with_recovery)))
            for entry in all_discard:
                entry.prepare_discard()
            if clear_structure and structure is not None:
                structure.clear()
            self._editor_close_release = (identity, all_discard)
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            self.logs.emit(str(exc))
            self._edit_restore_beat_recording(saved_beat_state)
            return False
        finally:
            self._editor_closing = False

    def _edit_close(self):
        self._notes_score_retire()
        approval = getattr(self, '_editor_close_release', None)
        self._editor_close_release = None  # consumed exactly once
        entries = live.owners()
        exact = approval is not None and approval[0] == self._close_identity(entries)
        timer = getattr(self, '_save_timer', None)
        if timer is not None:
            timer.stop()
        for entry in entries:
            live.release(entry.project, discard=bool(exact and entry in approval[1]))

    def _edit_scope(self, data, project, doc):
        scope = data.get('editor_scope')
        if scope is None:
            return
        stamp = doc.get('edit_stamp') or {}
        allow_audio = bool(isinstance(scope, dict) and scope.get('allow_audio_audition'))
        if (not isinstance(scope,dict) or not stamp or scope.get('project')!=project
                or scope.get('generation')!=stamp.get('generation')
                or scope.get('version')!=stamp.get('version') or scope.get('source_key')!=doc.get('source_key')
                or (not allow_audio and not self._edit_preview_selected(doc))):
            raise ValueError('工程、编辑音源或编辑版本已变化；此操作未执行。')

    def _gesture_context(self, data, op, doc, upstream):
        context = {}
        view, cmd = op.get('context'), op['command']
        key = view.get('detected_cache_key') if isinstance(view, dict) else None
        if key is not None:
            if not isinstance(key, str) or not key:
                raise ValueError('无效的检测拍引用。')
            context['detected_seconds'] = _gesture_detection(self._project.get('detected_beats'), doc, key)
        if 'cc_settle' in data:
            phase = data['cc_settle']
            if (cmd['action'] != 'cc_drag' or 'line_id' in cmd or not upstream or
                    type(phase) not in (int, float) or not 0 <= phase <= 1):
                raise ValueError('踏板显示起点不再有效。')
            # The current editor_after receipt already proves the owner/head.
            # Ease is presentation evidence, not seconds, value or an ID map.
            ghosts = (upstream.get('semantic_preview') or {}).get('ghosts') or []
            adds = upstream.get('added_ids') or []
            wanted = {i for i in cmd.get('ids', []) if isinstance(i, str)} if isinstance(cmd.get('ids'), list) else set()
            channel = cmd.get('channel', 0)
            actual = {c['id']: c for c in doc['controls'] if isinstance(c.get('id'), str)
                      and c['id'] in wanted and c['number'] == 64 and c.get('channel', 0) == channel}
            display = {}
            for g in ghosts:
                i = g.get('add_index')
                ident = g.get('id') if i is None else adds[i] if type(i) is int and 0 <= i < len(adds) else None
                if ident not in actual:
                    continue  # Not selected, or a native collision loser.
                target = actual[ident]
                display[ident] = {k: g[k] + (target[k] - g[k]) * phase for k in ('seconds', 'value')}
            if display:
                context['cc_display'] = display
        return dict(semantic_context=context) if context else None

    def _preview_selection_range(self, data, doc):
        self._edit_identity()
        project = self._edit_project_path
        base = data.get('base_revision')
        editing._check_base(doc, base)
        if not data.get('editor_scope') or 'cc_settle' in data:
            raise ValueError('选区预览缺少当前编辑视图身份。')
        self._edit_scope(data, project, doc)
        op = data['operation']
        return live.preview(project, base, op, expected=self._editor_native_stamp,
                            trusted=self._gesture_context(data, op, doc, None))

    def _edit_submit(self, action, data):
        self._edit_init()
        generation = self._edit_identity()
        project = self._edit_project_path
        request = str(data.get('editor_request') or uuid4().hex)[:160]
        prior = self._edit_receipts.get(request)
        if prior and prior[0] is generation:
            self.editorTransactionChanged.emit(self._json(prior[1]))
            return
        doc = (self._project or {}).get('editor') or {}
        base = data.get('base_revision')
        receipt = dict(request=request,action=action,project=project,pending=False,status='failed',
                       revision=None,error=None,applied=False)
        applied = None
        delta = None
        publication = None
        checked_scope = False
        try:
            if self._closed or getattr(self,'_editor_closing',False) or not doc:
                raise ValueError('没有可编辑的工程会话。')
            # L/R locators only change the head's range (no new revision, no
            # note change), so they stay usable while a score job runs.
            operation = data.get('operation')
            locator_only = action=='setEditorRange' or (action=='editProject'
                and isinstance(operation,dict) and operation.get('type')=='range')
            if (self.jobs.running or self._gpu_request) and not locator_only:
                raise ValueError('请等待当前工程任务结束后再编辑。')
            editing._check_base(doc,base)
            expected = tuple(self._editor_native_stamp)
            self._edit_scope(data,project,doc)
            checked_scope = bool(data.get('editor_scope'))
            dependency = data.get('editor_after')
            upstream = None
            if dependency is not None:
                upstream = self._edit_receipts.get(str(dependency))
                if (not upstream or upstream[0] is not generation or not upstream[1]['applied']
                        or upstream[1].get('warning') or upstream[1]['revision']!=base
                        or tuple(upstream[1]['native_stamp'])!=expected):
                    raise ValueError('前一步编辑不再是当前安装；后续操作未执行。')
            if action in ('undoProject','redoProject'):
                op = dict(type='history',direction='undo' if action=='undoProject' else 'redo')
            elif action=='setEditorRange':
                op = dict(data.get('range',data),type='range')
            else:
                op = data.get('operation',{})
                if not isinstance(op,dict) or op.get('type') in ('history','draft_apply','refresh_candidate'):
                    raise ValueError('请使用相应的编辑入口。')
            cmd = op.get('command')
            semantic = op.get('type') == 'note_semantic'
            gesture = semantic and isinstance(cmd, dict) and cmd.get('action') in _GESTURE_ACTIONS
            range_edit = op.get('type') == 'selection_range'
            pedal_length = semantic and isinstance(cmd, dict) and cmd.get('action') == 'pedals_to_note_length'
            if pedal_length and (not checked_scope or dependency is not None or 'cc_settle' in data):
                raise ValueError('踏板转时值需要当前音符选区；请重新选择。')
            trusted = None
            if range_edit:
                if not checked_scope or not isinstance(cmd, dict) or 'cc_settle' in data:
                    raise ValueError('选区操作缺少当前编辑视图身份。')
                if dependency is not None and (not upstream[1].get('selection_edit') or
                        doc['edit_stamp']['request'] != dependency):
                    raise ValueError('选区确认已更新；未执行后续操作。')
                trusted = self._gesture_context(data, op, doc, None)
            if gesture:
                if not checked_scope:
                    raise ValueError('手势缺少当前编辑视图身份。')
                prior_receipt = upstream[1] if upstream else None
                if dependency is not None and (cmd['action'] != 'cc_drag' or doc['edit_stamp']['request'] != dependency):
                    raise ValueError('后续踏板拖动不再基于当前确认。')
                trusted = self._gesture_context(data, op, doc, prior_receipt)
            self._cancel_recent_open()
            entry = live.owner(project)
            applied = live.apply(project,base,op,expected=expected,**({'trusted':trusted} if trusted else {}))
            head = applied['head']
            receipt.update(status='committed', applied=True, revision=head['revision'], native_stamp=head['stamp'],
                           applied_sequence=head['applied_sequence'], changed=applied['changed'])
            result = applied['delta']
            events = result.get('events')
            if range_edit:
                ids = dict(notes=[], controls=[])
                added = dict(notes=[], controls=[])
                for batch in result.get('event_batches', ()):
                    g = batch['group']
                    ids[g].extend(item['row']['id'] for item in batch['upsert'])
                    added[g].extend(item['id'] for item in batch['additions'] if item['retained'])
                receipt.update(selection_edit=True, selection_range=head.get('selection_range'),
                               range_ids=ids, range_added_ids=added,
                               semantic_preview=result['semantic_preview'], semantic_warnings=result['semantic_warnings'])
            if semantic or events and op.get('type') in ('notes','controls'):
                receipt['added_ids'] = [a['id'] if a['retained'] else None
                                        for a in (events or {}).get('additions',())]
            if semantic:
                receipt.update(semantic_preview=applied['delta']['semantic_preview'],
                               semantic_warnings=applied['delta']['semantic_warnings'])
                if events and events['group'] == 'controls':
                    receipt['semantic_replaced'] = (len(events['normalized'])
                        - sum(not a['retained'] for a in events['additions']))
                if gesture and cmd['action'].startswith('cc_') and events:
                    normalized = {r['id'] for r in events['normalized'] if 'id' in r}
                    overwritten = receipt['semantic_preview'].get('overwrite_count', 0)
                    receipt['cc_replaced'] = overwritten + len(normalized.intersection(events['remove']))
            if generation is self._edit_identity() and tuple(self._editor_native_stamp)==expected:
                old_stamp, old_count = doc['edit_stamp'], len(doc['notes'])
                delta = applied['view_delta']
                self._install_memory_edit(entry,doc,head,delta,request)
                scope = data.get('editor_scope') or {}
                splice = delta.get('rows') or {}
                writes = splice.get('write',[])
                note_insert = op.get('type')=='notes' or gesture and cmd['action']=='note_create'
                if (scope and action=='editProject' and note_insert and data.get('note_delta')==1
                        and splice.get('group')=='notes' and not splice.get('erase') and len(writes)==1
                        and writes[0][:2]==[old_count,True] and receipt.get('added_ids')==[writes[0][2]['id']]):
                    publication = dict(base_revision=base,base_version=old_stamp['version'],source_key=scope['source_key'],
                        count=old_count+1,note=writes[0][2],fields={k:doc[k] for k in _NOTE_FIELDS if k in doc})
        except Exception as exc:
            if applied is not None or isinstance(exc,nd.CommittedEditError):
                head = applied['head'] if applied is not None else exc.index
                receipt.update(status='committed',applied=True,revision=head['revision'],native_stamp=head['stamp'],
                               applied_sequence=head['applied_sequence'],warning='编辑已应用；显示更新失败，请刷新，不要重试编辑：'+str(exc))
                if generation is self._edit_identity():
                    try:
                        self._refresh_editor()
                    except Exception as refresh:
                        self.logs.emit(str(refresh))
                delta = publication = None
            else:
                receipt['error'] = str(exc) or type(exc).__name__
                if checked_scope and doc and self._editor_native_stamp is not None:
                    entry = live.retained(project) if project else None
                    receipt['base_current'] = bool(entry and entry.head['revision']==base
                        and tuple(entry.head['stamp'])==tuple(self._editor_native_stamp))
        self._edit_receipts[request] = generation, receipt
        while len(self._edit_receipts)>128:
            self._edit_receipts.pop(next(iter(self._edit_receipts)))
        current = generation is self._edit_identity() and project==self._edit_project_path
        if current:
            self._editor_transaction = receipt
            # While a job runs its banner belongs to that job; an edit refused
            # meanwhile is reported by its receipt (editor notice), not as a
            # job failure that vanishes when the job publishes again.
            job_busy = self.jobs.running or self._gpu_request
            if not data.get('editor_scope') and not (job_busy and receipt['error']):
                self._error = receipt['error'] or receipt.get('warning')
        wire = dict(receipt)
        if current and publication is not None:
            wire['note_publication'] = publication
        elif current and data.get('editor_delta') is True and delta is not None and not delta.get('replace'):
            if 'tempo' in delta.get('set', {}):
                delta = dict(delta, set=dict(delta['set'], range_grid_sections=self._project['editor']['range_grid_sections']))
            wire['editor_delta'] = dict(delta, base_revision=base, base_stamp=list(expected),
                head=applied['head'], edit_stamp=self._project['editor']['edit_stamp'],
                dirty=self._project['editor']['dirty'], score_stale=self._project['editor']['score_stale'])
        self.editorTransactionChanged.emit(self._json(wire))
        if current and 'note_publication' not in wire and 'editor_delta' not in wire:
            self._publish()

    def _install_memory_edit(self, entry, doc, head, patch, request):
        self._notes_score_musical_edit(entry,head)
        old_revision, old_range, old_tempo = doc['revision'],doc.get('range'),doc.get('tempo')
        frozen = None
        if patch.get('replace'):
            frozen = entry.freeze(head['stamp'])
            body = entry.snapshot_view(frozen)
            for name in ('path','preview_path','preview_revision','preview_error','metronome','audition_together'):
                if name in doc:
                    body[name] = doc[name]
            doc = self._project['editor'] = body
        else:
            for name in patch.get('unset',[]):
                doc.pop(name,None)
            doc.update(patch.get('set',{}))
            splice = patch.get('rows')
            for part in ([splice] if splice else patch.get('row_groups', ())):
                rows = doc[part['group']]
                # Publish a new list identity for gesture and renderer caches.
                if 'row_groups' in patch or head.get('selection_range') is not None:
                    rows = doc[part['group']] = list(rows)
                for i in reversed(part['erase']):
                    del rows[i]
                for i, insert, row in part['write']:
                    if insert:
                        rows.insert(i,row)
                    else:
                        rows[i] = row
            tombs = splice or patch
            if 'deleted_prefix' in tombs:
                deleted = doc['deleted_ids']
                del deleted[tombs['deleted_prefix']:]
                deleted.extend(tombs['deleted_tail'])
        if 'selection_range' in head:
            doc['selection_range'] = head['selection_range']
        else:
            doc.pop('selection_range', None)
        doc.update(revision=head['revision'],can_undo=head['can_undo'],can_redo=head['can_redo'],
                   range=head['range'],duration=head['duration'],preview_pending=False,
                   preview_revision=head['revision'],exported_revision=entry.marks['exported_revision'],
                   dirty=entry.marks['exported_revision']!=head['revision'],
                   score_stale=entry.marks['score_revision']!=head['revision'])
        if old_tempo is not doc['tempo'] or 'range_grid_sections' not in doc:
            doc['range_grid_sections'] = range_grid_view(doc['tempo'])
        self._project['tempo'] = doc['tempo']
        self._editor_native_stamp = tuple(head['stamp'])
        self._edit_stamp(doc,request)
        if old_range != doc['range']:
            self._audition_range = None
        if self._edit_preview_selected(doc) and getattr(self,'_workspace_midi',None):
            if old_revision!=doc['revision'] or old_range!=doc['range']:
                self._workspace_midi.set_workspace(frozen or entry.freeze(head['stamp']))
                self._sync_editor_transport(range_only=old_tempo is doc['tempo'])
        elif old_tempo is not doc['tempo']:
            refresh_click = getattr(self, '_refresh_audio_tempo_click', None)
            if callable(refresh_click):
                refresh_click()
        self._edit_emit_save_status()

    def _select_live_editor(self, value, resume=None):
        doc = (self._project or {}).get('editor') or {}
        if not doc or value!=self._editor_uri or value!=doc.get('preview_path'):
            raise ValueError('编辑音源已切换。')
        if self._edit_preview_selected() and resume is None:
            return
        from .audio_output.applied_workspace import WorkspaceMidiAdapter
        prior=getattr(self,'_workspace_midi',None)
        if prior is not None:prior.close()
        generation,project=self._edit_identity(),self._edit_project_path
        def current(key):
            return (not self._closed and generation is self._edit_generation and
                    self._edit_preview_selected() and key[0]==live.key(project) and
                    key[1]==tuple(self._editor_native_stamp))
        frozen=live.playback_snapshot(project,expected=self._editor_native_stamp)
        adapter=WorkspaceMidiAdapter(self,frozen,value,current)
        self._workspace_midi=adapter
        try:
            self.select_memory_midi(adapter.epoch,position=resume[0] if resume is not None else 0.)
            # select_memory_midi clears old file transport first.
            self._midi_file=dict(path=value,name='编辑版本',notes=[])
            doc.pop('preview_error',None)
            self._sync_editor_transport()
            if resume is not None:
                self._output_change(**self._midi_selection_resume(resume))
        except Exception:
            adapter.close();raise
        self._notify_midi()

    def _bind_working_export(self, request):
        """Only explicit reexport; the subprocess gets a value, not a live handle."""
        if request.get('kind')!='reexport' or not (self._project or {}).get('editor'):
            return request
        if live.key(request['project'])!=live.key(self._project['path']):
            return request
        entry = live.owner(request['project'])
        doc = self._project['editor']
        base = request.get('editor_revision',doc['revision'])
        entry.check(base,self._editor_native_stamp)
        options = dict(request['options'])
        export_options = dict(request.get('editor_export_options') or {})
        factor, path, bpm = options.get('beat_scale',1.),options.get('tempo_path'),options.get('bpm')
        if path or bpm is not None or factor!=1:
            from .pipeline import _load_tempo
            from .timing import fixed_tempo, scale_tempo
            if path or bpm is not None:
                tempo = _load_tempo(path,doc['duration']) if path else fixed_tempo(bpm,doc['duration'],options.get('meter','4/4'))
                op = dict(type='replace_tempo',tempo=scale_tempo(tempo,factor))
            else:
                op = dict(type='tempo',action='interpret',start=0.,end=doc['duration'],factor=factor)
            receipt = live.apply(entry.project,base,op,expected=self._editor_native_stamp)
            self._install_memory_edit(entry,doc,receipt['head'],receipt['view_delta'],None)
            base = receipt['revision']
            options.update(tempo_path=None,bpm=None,beat_scale=1.)
            export_options['_working_clock_override'] = True
        capture=entry.export_input(base,self._editor_native_stamp)
        export_options['_working_input'] = capture
        bound=dict(request,options=options,editor_revision=base,editor_export_options=export_options)
        if bound.pop('_notes_score_update',False):
            bound['provisional_score']=self._notes_score_begin(entry,capture)
        return bound
