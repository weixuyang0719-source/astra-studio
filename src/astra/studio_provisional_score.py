"""One full score update bound to the current live Notes owner, then one formal save."""
from threading import Event
from uuid import uuid4

from PySide6.QtCore import QThreadPool, Qt, Slot
from . import live_owner, provisional_score as draft
from .preview_cancel import cancellation_scope


class ProvisionalScoreBridge:
    def _notes_score_retire(self):
        if scope := getattr(self, '_notes_score', None):
            scope['cancel'].set()
            self._notes_score = None

    def _notes_score_begin(self, entry, capture):
        self._notes_score_retire()
        head = capture['head']
        token = uuid4().hex
        self._notes_score = dict(token=token, project=self._project, owner=entry,
            generation=self._edit_identity(), epoch=head['stamp'][0],
            revision=head['revision'], source=head['source_key'],
            lineage=capture['guard']['lineage'], sequence=head['applied_sequence'],
            cancel=Event(), phase='building')
        return token

    def _notes_score_current(self, scope):
        if (scope is None or getattr(self,'_notes_score',None) is not scope or scope['cancel'].is_set()
                or getattr(self,'_closed',False) or self._project is not scope['project']
                or getattr(self,'_edit_generation',None) is not scope['generation']):
            return False
        entry = scope['owner']
        if live_owner.retained(entry.project) is not entry:
            return False
        with entry.lock:
            head = entry.head
            return (not entry.closed and head['stamp'][0] == scope['epoch']
                    and (head['source_key'], head['revision']) == (scope['source'], scope['revision'])
                    and entry.saves.writer.lineage == scope['lineage'])

    def _notes_score_musical_edit(self, entry, head):
        scope = getattr(self, '_notes_score', None)
        if scope and scope['owner'] is entry and (head['revision'],head['source_key']) != (scope['revision'],scope['source']):
            self._notes_score_retire()
            self._phase = '音符或拍点已变化，本次未更新谱面'

    def _notes_score_status(self):
        scope = getattr(self,'_notes_score',None)
        if not self._notes_score_current(scope):
            return None
        return dict(token=scope['token'], project=scope['project']['path'],
                    revision=scope['revision'], phase=scope['phase'], message=scope.get('message',''))

    def _notes_score_save_in_progress(self):
        scope = getattr(self,'_notes_score',None)
        return self._notes_score_current(scope) and scope['phase'] in ('building','preparing','ready')

    def _notes_score_task(self, row, scope):
        from .studio_bridge import _InputInspectionSignal, _InputInspectionTask
        if not hasattr(self,'_notes_score_signal'):
            self._notes_score_signal=_InputInspectionSignal(self)
            self._notes_score_signal.done.connect(self._notes_score_done,Qt.ConnectionType.QueuedConnection)
        def prepare(data):
            with cancellation_scope(scope['cancel']):
                receipt = draft.capture(data)
                return receipt, draft.prepare_publication(receipt)
        QThreadPool.globalInstance().start(_InputInspectionTask(row,scope['token'],self._notes_score_signal,prepare))

    def _notes_score_error(self, scope, error):
        message = str(error) or type(error).__name__
        self.logs.emit('更新谱面失败：'+message)
        scope.update(phase='failed', message=message)
        self._phase='更新谱面失败 · 原谱未改变'
        self._error=message
        self._publish()

    def _notes_score_completed(self, row):
        scope = getattr(self, '_notes_score', None)
        if row.get('token') != (self._request or {}).get('provisional_score'):
            return
        self._job_progress.finish()
        if not self._notes_score_current(scope) or row.get('token') != scope['token'] or scope['phase'] != 'building':
            self._publish()
            return
        keys = ('token','source','revision','lineage','sequence')
        if (tuple(row.get(k) for k in keys) != tuple(scope[k] for k in keys)
                or not isinstance(row.get('project'),str) or live_owner.key(row['project']) != scope['owner'].project):
            self._notes_score_error(scope,'本次生成的谱面与工程不匹配。')
            return
        scope['phase']='preparing'
        self._phase='正在渲染并保存新谱面…'
        self._notes_score_task(row,scope)
        self._publish()

    def _notes_score_terminal(self):
        scope=getattr(self,'_notes_score',None)
        if scope and (self._request or {}).get('provisional_score')==scope['token'] and scope['phase']=='building':
            self._notes_score_retire()

    @Slot(str,object,str)
    def _notes_score_done(self, token, result, error):
        scope = getattr(self, '_notes_score', None)
        if not self._notes_score_current(scope) or token != scope['token'] or scope['phase'] != 'preparing':
            return
        if error:
            self._notes_score_error(scope,error)
            return
        scope.update(phase='ready', prepared=result)
        self._notes_score_save_check()

    def _notes_score_save_check(self, closing=False):
        scope = getattr(self, '_notes_score', None)
        if not self._notes_score_current(scope) or scope['phase'] != 'ready' or self.jobs.running or self._gpu_request:
            return
        entry = scope['owner']
        receipt, proof = scope['prepared']
        try:
            # This is the only commit. Keep the same native session/history and
            # freeze the exact Notes, beat points, and currently attached state.
            with entry.lock:
                if not self._notes_score_current(scope):
                    return
                result = entry.saves.write_score(entry._save_snapshot(), receipt, proof)
            if result == 'busy':
                return
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if getattr(exc, 'outcome', None) == 'indeterminate':
                self.logs.emit('无法确认正式检查点的写入结果：'+str(exc))
                scope.update(phase='failed', message=str(exc))
                self._phase='未能确认更新谱面的保存结果'
                self._error=str(exc)
                self._publish()
            else:
                self._notes_score_error(scope,exc)
            return
        uncertainty = '谱面和音符已一起更新 · 未能确认磁盘最终写入' if result == 'committed_unconfirmed_durability' else None
        if uncertainty:
            self.logs.emit(uncertainty)
        try:
            self._adopt_editor_score_export(receipt.project, receipt.revision)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            self.logs.emit('已连同音符保存，但无法刷新谱面显示：'+str(exc))
            notice='谱面和音符已保存 · 重新打开工程后查看'
        else:
            notice='谱面、音符和拍点已保存'
        self._notes_score_retire()
        self._phase=uncertainty or notice
        self._error=None
        self._publish()
