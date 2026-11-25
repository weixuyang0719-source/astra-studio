"""Ordered disk editing off the Qt event loop; completion is a separate protocol."""
from copy import deepcopy
from os import environ
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot, Qt
from . import editing
from .studio_edit_preview import EditorPreviews

ACTIONS = frozenset(('editProject','undoProject','redoProject','setEditorRange'))
# These commands neither replace nor mutate the working project on disk.
DURING_EDIT = frozenset(('playPause','stop','seek','volume','midiPlayPause','midiStop',
    'midiSeek','midiVolume','midiSetRange','selectMidi','selectAudio','audition','selectAuditionSource',
    'auditionRate','setAuditionRate','auditionTogether','metronome','selectPdf'))

# Only these small view fields can differ in an append publication. The rest
# must still be the installed base (apart from a retired preview error).
_NOTE_FIELDS = ('revision','edit_stamp','operation','manual','can_undo','can_redo',
                'dirty','score_stale','exported_revision','preview_pending','metronome','audition_together')
_NOTE_MUTABLE_KEYS = frozenset((*_NOTE_FIELDS,'notes','preview_error'))
_NOTE_PREVIEW_GRACE = .10

def _rapid_note_packet(packet):
    if packet and packet['action']=='editProject':
        data=packet['data']
        return (type(data.get('note_delta')) is int and data['note_delta']==1
                and isinstance(data.get('editor_scope'),dict)
                and editing._plain_note_add(data.get('operation')) is not None)
    return False

def _note_publication(packet,previous,doc,receipt):
    if (not _rapid_note_packet(packet) or packet.get('review_changed') or not doc
            or receipt.get('warning') or receipt.get('audition_warning') or receipt.get('error')):
        return None
    old,rows=previous.get('notes'),doc.get('notes')
    base=packet['data']['base_revision']
    if (previous.get('revision')!=base or doc['revision']==base or type(old) is not list
            or type(rows) is not list or len(rows)!=len(old)+1 or receipt.get('added_ids') != [rows[-1]['id']]):
        return None
    # Cooperative writers never rewrite a revision body. Still check the
    # visible prefix and all retained fields: the just-read index can carry an
    # external range/export change, or a legacy view migration can differ.
    static=previous.keys()-_NOTE_MUTABLE_KEYS
    if (static != doc.keys()-_NOTE_MUTABLE_KEYS or any(previous[k]!=doc[k] for k in static)
            or any(k in previous and k not in doc for k in _NOTE_FIELDS) or rows[:-1]!=old):
        return None
    scope=packet['data']['editor_scope']
    return dict(base_revision=base,base_version=scope['version'],source_key=scope['source_key'],
                count=len(rows),note=rows[-1],fields={k:doc[k] for k in _NOTE_FIELDS if k in doc})

def _range_edit(packet):
    operation = packet['data'].get('operation')
    return packet['action']=='setEditorRange' or (packet['action']=='editProject'
        and isinstance(operation,dict) and operation.get('type')=='range')

def _execute_edit(packet):
    """No bridge or Qt media objects in this function. CAS stays inside file locks."""
    project, action, data = packet['project'], packet['action'], packet['data']
    if action not in ACTIONS: raise ValueError('Unknown edit transaction action')
    base = data.get('base_revision')
    if action == 'editProject':
        operation = data.get('operation', {})
        doc = (editing.set_range(project, base, operation) if operation.get('type') == 'range'
               else editing.edit(project, base, operation))
    elif action == 'setEditorRange':
        doc = editing.set_range(project, base, data.get('range', data))
    else:
        doc = editing.navigate(project, base, 'undo' if action == 'undoProject' else 'redo')
    return doc

def execute_edit(packet):
    """Observe the authoritative replace; never retry a committed operation."""
    committed = None
    target = Path(packet['project'])/'editing'/'current.json'
    operation = packet['data'].get('operation') if packet['action']=='editProject' else None
    kind = operation.get('type') if isinstance(operation,dict) else None
    add = operation.get('add',[]) if kind in ('notes','controls') else []
    def observe(path, index):
        nonlocal committed
        if path == target: committed = index.copy()
    token = editing._commit_observer.set(observe)
    additions = {}
    added_token = editing._added_event_observer.set(lambda group,i,ident:additions.__setitem__((group,i),ident))
    def outcome(doc, warning=None):
        result = dict(doc=doc, index=committed, commit_error=warning,
                      revision=committed['revision'] if committed is not None else doc['revision'])
        if kind in ('notes','controls'):
            # Only scan for retained IDs on additions. No positional guessing.
            retained = {e['id'] for e in doc[kind]} if doc and additions else set()
            result['added_ids'] = [ident if ident in retained else None
                for i in range(len(add)) for ident in [additions.get((kind,i))]]
        return result
    try:
        return outcome(_execute_edit(packet))
    except Exception as exc:
        if committed is None: raise
        warning = str(exc) or type(exc).__name__
        try:
            try: doc = editing.current(packet['project'],expected_index=committed)
            except Exception: doc = None
            return outcome(doc,warning)  # retain positional nulls if IDs cannot be verified
        except Exception:
            # Even a failed recovery or ID projection cannot undo publication.
            return dict(doc=None,index=committed,revision=committed['revision'],commit_error=warning)
    finally:
        editing._commit_observer.reset(token)
        editing._added_event_observer.reset(added_token)

class _Done(QObject):
    ready = Signal(object, object, str)
    preview = Signal(object, object, str)  # ticket, prepared asset, error
    install_preview = Signal()  # one coalesced GUI turn, not one timer per revision

class _Task(QRunnable):
    def __init__(self, packet, signal):
        super().__init__(); self.packet, self.signal = packet, signal

    def run(self):
        start = perf_counter() if self.packet.get('queued_at') is not None else None
        try: result, error = execute_edit(self.packet), ''
        except Exception as exc:
            result, error = None, str(exc) or type(exc).__name__
            if self.packet['data'].get('editor_scope') is not None:
                # Only on failure, off the GUI thread. Do not continue a burst
                # on a stale CAS, or retry the failed request itself.
                try:
                    head = editing.current(self.packet['project'])
                    self.packet['base_current'] = bool(head and head['revision'] == self.packet['data']['base_revision'])
                except Exception:
                    pass
        if start is not None: self.packet['disk_times'] = (start,perf_counter())
        try: self.signal.ready.emit(self.packet, result, error)
        except RuntimeError: pass

class EditTransactions(EditorPreviews):
    def _edit_init(self):
        if hasattr(self, '_edit_pool'): return
        self._edit_pool = QThreadPool(self); self._edit_pool.setMaxThreadCount(1)
        self._edit_preview_pool = QThreadPool(self); self._edit_preview_pool.setMaxThreadCount(1)
        self._edit_done_signal = _Done(self)
        self._edit_done_signal.ready.connect(self._edit_done, Qt.ConnectionType.QueuedConnection)
        self._edit_done_signal.preview.connect(self._edit_preview_done, Qt.ConnectionType.QueuedConnection)
        self._edit_done_signal.install_preview.connect(self._edit_preview_install, Qt.ConnectionType.QueuedConnection)
        self._edit_queue = []; self._edit_active = None; self._edit_receipts = {}
        self._edit_preview_active = self._edit_preview_waiting = None
        self._edit_preview_latest = self._edit_preview_ready = None
        self._edit_preview_posted = False
        self._edit_trace = environ.get('ASTRA_EDIT_TRACE') == '1'

    def _edit_identity(self):
        # A strong reference prevents id reuse; the worker receives only a
        # generation token and a path, not a reference to mutable bridge state.
        path = str((self._project or {}).get('path',''))
        if (not hasattr(self,'_edit_generation') or self._edit_project_ref is not self._project
                or self._edit_project_path != path):
            if hasattr(self,'_edit_generation'):
                self._cancel_midi_intent()
            self._edit_project_ref = self._project
            self._edit_project_path = path
            self._edit_generation = object()
            self._edit_generation_number = getattr(self,'_edit_generation_number',0) + 1
            self._editor_transaction = None
            self._editor_preview_clock = None
            self._edit_preview_invalidate()
        return self._edit_generation

    def _edit_matches(self, packet):
        return (packet['generation'] is self._edit_identity()
                and packet['project'] == str((self._project or {}).get('path','')))

    def _edit_stamp(self, doc, request=None):
        # View metadata only. Revisions identify disk contents and can recur
        # on undo; this number identifies an installation in this bridge.
        self._edit_identity()
        self._edit_publication_number = getattr(self,'_edit_publication_number',0) + 1
        doc['edit_stamp'] = dict(generation=self._edit_generation_number,
                                 version=self._edit_publication_number,request=request)

    def _edit_check_scope(self, packet):
        data = packet['data']
        scope = data.get('editor_scope')
        if scope is None: return
        doc = (self._project or {}).get('editor') or {}
        stamp = doc.get('edit_stamp') or {}
        if (not isinstance(scope,dict) or not stamp or scope.get('project') != packet['project']
                or scope.get('generation') != self._edit_generation_number
                or scope.get('generation') != stamp.get('generation')
                or scope.get('version') != stamp.get('version')
                or scope.get('source_key') != doc.get('source_key')
                or data.get('base_revision') != doc.get('revision')
                or not self._edit_preview_selected(doc)):
            raise ValueError('工程、编辑音源或保存版本已变化；此操作未执行。')
        operation = data.get('operation')
        if packet['action'] in ('undoProject','redoProject'): return
        if (packet['action'] != 'editProject' or not isinstance(operation,dict)
                or operation.get('type') != 'notes' or set(operation) != {'type','add'}
                or not isinstance(operation['add'],list) or len(operation['add']) != 1):
            raise ValueError('音符写入队列只接受独立的单音新增操作。')

    def _edit_pending(self):
        return bool(getattr(self, '_edit_active', None) or getattr(self, '_edit_queue', []))

    def _edit_revision_pending(self):
        return any(not _range_edit(p) for p in
                   (getattr(self,'_edit_active',None), *getattr(self,'_edit_queue',())) if p)

    def _edit_submit(self, action, data):
        self._edit_init()
        client_request = data.get('editor_request')
        request = str(client_request or uuid4().hex)[:160]
        generation = self._edit_identity()
        project = str((self._project or {}).get('path',''))
        receipt_generation, receipt = self._edit_receipts.get(request, (None, {}))
        if receipt_generation is generation and receipt.get('project') == project:
            self.editorTransactionChanged.emit(self._json(receipt)); return
        if any(p['request'] == request and p['generation'] is generation
               and p['project'] == project
               for p in [self._edit_active, *self._edit_queue] if p): return
        packet = {'request':request,'action':action,
                  'project':project,
                  'generation':generation,'data':deepcopy(data)}
        if self._edit_trace: packet['queued_at'] = perf_counter()
        try:
            if self._closed: raise ValueError('窗口正在关闭；未接受编辑。')
            if not packet['project']: raise ValueError('请先打开工程。')
            self._edit_check_scope(packet)
            dependency = data.get('editor_after')
            if dependency is not None:
                generation, upstream = self._edit_receipts.get(str(dependency),(None,{}))
                if (generation is not packet['generation'] or upstream.get('status') != 'committed' or upstream.get('warning')
                    or upstream.get('project') != packet['project']
                    or upstream.get('revision') != data.get('base_revision')):
                    raise ValueError('前一步编辑尚未确认成功，后续拖动未保存；请检查当前结果后重试。')

            # L/R locators are view state on the head (no new revision, no
            # note change), so moving them never invalidates a running job.
            if (self.jobs.running or self._gpu_request) and not _range_edit(packet):
                raise ValueError('请等待当前工程任务结束后再编辑。')
            # Scoped entry has exactly one submitted head. Never leave it in
            # the generic queue behind a different mutation or history command.
            if data.get('editor_scope') is not None and self._edit_pending():
                raise ValueError('另一项编辑仍在保存；新操作未执行。')
            if len(self._edit_queue) >= 32: raise ValueError('待保存编辑过多，请稍候。')
            if not self._edit_pending():
                prior_notice=getattr(self,'_review_notice',None)
                self._require_no_structure_draft()
                packet['review_changed']=prior_notice!=getattr(self,'_review_notice',None)
        except Exception as exc:
            self._edit_finish(packet, None, str(exc)); return
        self._cancel_recent_open()
        self._edit_queue.append(packet)
        self._edit_start()

    def _edit_start(self):
        if self._closed or self._edit_active or not self._edit_queue: return
        self._edit_active = self._edit_queue.pop(0)
        try:
            if not self._edit_matches(self._edit_active):
                raise ValueError('工程已切换；排队编辑未执行。')
            self._edit_check_scope(self._edit_active)
        except ValueError as exc:
            packet, self._edit_active = self._edit_active, None
            self._edit_finish(packet, None, str(exc))
            self._edit_start()
            return
        self._editor_transaction = {k:self._edit_active[k] for k in ('request','action','project')}
        self._editor_transaction.update(status='pending', pending=True)
        self._edit_active['visible_revision'] = ((self._project or {}).get('editor') or {}).get('revision')
        # Fence audition on acceptance, not on receipt: Play can arrive while
        # the writer is still running. Range-only writes do not replace notes.
        try:
            if not _range_edit(self._edit_active): self._hold_editor_midi()
        except Exception as exc:
            self._edit_active['audition_warning'] = 'MIDI 暂停失败：' + (str(exc) or type(exc).__name__)
            if self._edit_active['data'].get('editor_scope') is None:
                self._problem(self._edit_active['audition_warning'])
        self._edit_pool.start(_Task(self._edit_active, self._edit_done_signal))

    @Slot(object, object, str)
    def _edit_done(self, packet, result, error):
        if packet is not self._edit_active: return
        self._edit_active = None
        if self._closed: return  # The disk commit is independent of the window.
        times = packet.get('disk_times')
        received = perf_counter() if times is not None else None
        try:
            self._edit_finish(packet, result, error)
        finally:
            self._edit_start()
            self._edit_preview_start()
            # A no-op does not need a preview job to release the fence.
            self._flush_midi_intent()
        if times is not None:
            import logging
            start,end = times
            logging.getLogger(__name__).warning(
                'Astra edit %s queue=%.1fms disk=%.1fms delivery=%.1fms gui=%.1fms',packet['action'],
                1000*(start-packet['queued_at']),1000*(end-start),1000*(received-end),1000*(perf_counter()-received))

    def _edit_preview_invalidate(self):
        self._note_preview_after=0.
        timer=getattr(self,'_note_preview_timer',None)
        if timer is not None: timer.stop()
        return super()._edit_preview_invalidate()

    @Slot()
    def _edit_preview_start(self):
        after=getattr(self,'_note_preview_after',0.)
        if after:
            if self._closed: return
            # _edit_done will retry after the active writer, even on failure.
            # Do not render the old head just because a quiet timer expired.
            if _rapid_note_packet(self._edit_active): return
            delay=after-perf_counter()
            intent=getattr(self,'_pending_midi_intent',None) or {}
            if delay>0 and not intent.get('playing'):
                timer=getattr(self,'_note_preview_timer',None)
                if timer is None:
                    timer=self._note_preview_timer=QTimer(self)
                    timer.setSingleShot(True)
                    timer.timeout.connect(self._edit_preview_start)
                timer.start(max(1,int(delay*1000)+1))
                return
            self._note_preview_after=0.
            timer=getattr(self,'_note_preview_timer',None)
            if timer is not None: timer.stop()
        return super()._edit_preview_start()

    def _edit_apply_result(self, packet, result):
        previous = self._project.get('editor') or {}
        doc = result['doc']
        if doc is None:
            self._edit_preview_invalidate()
            self._editor_preview_clock = None
            self._project.pop('editor',None)
            self._project['editor_unavailable'] = '编辑已保存，重新读取失败，请重新打开工程。'
            if self._edit_preview_selected(previous): self._clear_midi()
            return
        changed = doc['revision'] != previous.get('revision')
        # Keep the previous path as selection identity, not playback authority.
        for key in ('path','preview_path','preview_revision','preview_pending','preview_error'):
            doc.pop(key,None)
            if key in previous and (key!='preview_error' or not changed): doc[key] = previous[key]
        if self._project.get('score_export') and self._project.get('score_revision')!=doc['revision']:
            doc['score_stale'] = True
        doc.update(metronome=bool(getattr(self,'_editor_metronome',False)),
                   audition_together=bool(getattr(self,'_audition_together',False)))
        self._edit_stamp(doc,packet['request'])
        self._project['editor'] = doc; self._project['tempo'] = doc['tempo']
        self._project.pop('editor_unavailable',None)
        range_edit = _range_edit(packet)
        if range_edit: self._audition_range = None
        if changed:
            doc['preview_pending'] = True
            # Wait briefly for the next already-resolved pen intent to reach Qt.
            # This affects only derived MIDI work, not receipt, CAS or undo.
            self._edit_preview_queue(packet,doc['revision'])
            self._note_preview_after=(perf_counter()+_NOTE_PREVIEW_GRACE
                if _rapid_note_packet(packet) and not result.get('commit_error')
                and not packet.get('audition_warning') else 0.)
            self._hold_editor_midi()
        # Do not rebuild the metronome or touch an export MIDI on a commit.
        # The new clock is applied with the matching asset, not with its receipt.
        if range_edit or doc.get('range')!=previous.get('range'):
            self._sync_editor_transport(range_only=True)

    def _edit_remember(self,packet,receipt):
        self._edit_receipts[packet['request']] = (packet['generation'],receipt)
        while len(self._edit_receipts)>128: self._edit_receipts.pop(next(iter(self._edit_receipts)))

    def _edit_finish(self, packet, result, error):
        receipt = {k:packet[k] for k in ('request','action','project')}
        receipt.update(status='committed' if result is not None else 'failed',pending=False,
                       revision=result['revision'] if result is not None else None,error=error or None)
        current = self._edit_matches(packet)
        scoped = packet['data'].get('editor_scope') is not None
        append_base = None
        if current and scoped and result is None and packet.get('base_current'):
            try:
                self._edit_check_scope(packet)
            except ValueError:
                pass
            else:
                receipt['base_current'] = True
        if result is not None:
            if 'added_ids' in result: receipt['added_ids'] = result['added_ids']
            if result.get('commit_error'):
                receipt['warning'] = '编辑已保存，但提交后刷新异常：' + result['commit_error']
            if current:
                visible_doc = self._project.get('editor') or {}
                visible = visible_doc.get('revision')
                # A refresh can install the same content revision again. For a
                # scoped result the exact installation must still own the slot.
                stamp = visible_doc.get('edit_stamp') or {}
                scope = packet['data'].get('editor_scope') or {}
                same_installation = not scoped or (stamp.get('generation') == scope.get('generation')
                    and stamp.get('version') == scope.get('version'))
                if same_installation and visible in (packet.get('visible_revision'),result['revision']):
                    try:
                        self._edit_apply_result(packet,result)
                        append_base = visible_doc
                    except Exception as exc:
                        receipt.setdefault('warning','编辑已保存，但工作台更新失败：' + (str(exc) or type(exc).__name__))
                elif scoped:
                    receipt.setdefault('warning','编辑已保存，但当前工作台版本已改变；请核对结果。')
        if current and self._edit_preview_selected() and (result is None or packet.get('audition_warning')):
            self._cancel_midi_intent()
        if packet.get('audition_warning'):
            receipt['audition_warning'] = packet['audition_warning']
        self._edit_remember(packet,receipt)
        if current:
            editor = (self._project or {}).get('editor') or {}
            # The scoped lane displays its own receipt/error. Do not replace the
            # workspace job banner (and roll DOM) for every success/failure in it.
            if not scoped:
                self._error = error or receipt.get('warning') or packet.get('audition_warning') or editor.get('preview_error') or None
            self._editor_transaction = receipt
        publication=None
        if append_base is not None:
            publication=_note_publication(packet,append_base,result['doc'],receipt)
        if publication is not None:
            # Carry independent publication evidence on the existing signal.
            # Only the requesting, delta-capable view uses it. Poll/replay keep
            # the ordinary terminal receipt and snapshot() remains complete.
            self.editorTransactionChanged.emit(self._json(dict(receipt,note_publication=publication)))
        else:
            self.editorTransactionChanged.emit(self._json(receipt))
            if current: self._publish()

    def _edit_close(self):
        # Discard not-started work. Join the one possible writer in each pool
        # before destroying Qt signal owners; do not terminate a disk writer.
        self._edit_preview_invalidate()
        self._pending_midi_intent = None
        if hasattr(self, '_edit_pool'):
            queued, self._edit_queue = self._edit_queue, []
            for packet in queued:
                receipt = {k:packet[k] for k in ('request','action','project')}
                receipt.update(status='failed',pending=False,revision=None,error='窗口已关闭；排队编辑未执行。',cancelled=True)
                self._edit_remember(packet,receipt)
                self.editorTransactionChanged.emit(self._json(receipt))
            self._edit_pool.waitForDone()
            self._edit_preview_pool.waitForDone()
            # Already posted completions can still be delivered, but no longer
            # own a slot. Neither worker needs an event-loop response to exit.
            self._edit_active = self._edit_preview_active = None
            self._editor_preview_clock = None
