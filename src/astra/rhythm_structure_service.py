"""One detached structural draft per desktop window, with revision-bound tokens."""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4


from .document_editor import StructureDraftSession


class _ReferenceStructureDraftSession:
    """Old detached implementation retained as a differential fixture only."""
    def __init__(self):
        self._active = None

    def clear(self):
        self._active = None

    def pending(self, project):
        return bool(self._active and self._active['project']==Path(project).resolve()
                    and any(op.get('type')!='preserve_positions' for op in self._active['draft']['operations']))

    def _response(self):
        active = self._active
        draft = active['draft']
        edit_count=sum(op.get('type')!='preserve_positions' for op in draft['operations'])
        fields = ('scope', 'preserve_unedited_positions', 'pulses', 'bars', 'labels',
                  'valid', 'conflicts', 'tempo', 'display', 'guidance', 'summary', 'impact')
        return dict(ok=True, token=active['token'], draft_version=active['version'],
                    base_revision=active['base_revision'],
                    operation_count=edit_count,
                    can_undo=bool(draft['operations']),can_redo=bool(active['redo']),
                    can_apply=bool(draft['valid'] and edit_count),
                    draft={key:deepcopy(draft[key]) for key in fields if key in draft})

    def handle(self, project, request):
        from . import editing
        from .rhythm_structure_draft import create_draft, apply_edit
        if not isinstance(request, dict):
            raise ValueError('结构编辑请求必须是对象。')
        project = Path(project).resolve()
        action = request.get('action')
        if action == 'begin':
            doc = editing.current(project)
            editing._check_base(doc, request.get('base_revision'))
            enabled = request.get('preserve_unedited_positions', True)
            if type(enabled) is not bool:
                raise ValueError('请明确是否保留未编辑位置。')
            # A failed new begin must not erase the previous recoverable draft.
            draft = create_draft(doc['tempo'], doc['revision'], scope=request.get('scope'),
                                 fixed_anchors=doc['anchors'], preserve_unedited_positions=enabled)
            self._active = dict(project=project, token=uuid4().hex, version=0,
                                base_revision=doc['revision'], draft=draft,
                                initial_preserve=enabled,redo=[],checkpoints={0:draft})
            return self._response()
        active = self._active
        if (active is None or active['project'] != project
                or request.get('token') != active['token']):
            raise ValueError('结构草稿已失效，请重新进入编辑；当前工程未被覆盖。')
        if type(request.get('draft_version')) is not int or request['draft_version'] != active['version']:
            raise ValueError('草稿已有更新，请使用最新草稿；旧操作未覆盖新内容。')
        if action == 'discard':
            self.clear()
            return dict(ok=True, discarded=True)
        doc = editing.current(project)
        editing._check_base(doc, active['base_revision'])
        if request.get('base_revision') != active['base_revision']:
            raise ValueError('草稿来源版本不符。')
        if action == 'edit':
            if len(active['draft']['operations']) >= 512:
                raise ValueError('本次草稿已达512项操作，请先应用或撤回后继续。')
            operation = request.get('operation')
            if not isinstance(operation, dict):
                raise ValueError('请选择具体的拍位或拍号操作。')
            draft = apply_edit(active['draft'], operation)
            count=len(draft['operations'])
            active['checkpoints']={n:d for n,d in active['checkpoints'].items() if n<count}
            if count%16==0:active['checkpoints'][count]=draft
            active['draft'] = draft
            active['redo'] = []
            active['version'] += 1
            return self._response()
        if action in ('undo','redo'):
            operations=deepcopy(active['draft']['operations'])
            undone=deepcopy(active['redo'])
            if action=='undo':
                if not operations:raise ValueError('草稿中没有可撤销的操作。')
                undone.append(operations.pop())
            else:
                if not undone:raise ValueError('草稿中没有可重做的操作。')
                operations.append(undone.pop())
            # Bound UI undo work: old immutable checkpoints share no client
            # references; at most fifteen edits are replayed, not the full song's
            # entire editing session on every Ctrl+Z.
            count=max(n for n in active['checkpoints'] if n<=len(operations))
            draft=active['checkpoints'][count]
            for operation in operations[count:]:draft=apply_edit(draft,operation)
            if len(operations)%16==0:active['checkpoints'][len(operations)]=draft
            active.update(draft=draft,redo=undone,version=active['version']+1)
            return self._response()
        if action == 'status':
            return self._response()
        if action == 'apply':
            if not self._response()['can_apply']:
                raise ValueError('请先完成结构草稿中的冲突修正，再应用。')
            # Replay only server-held operations under editing.edit's lock/CAS.
            # Client-supplied tempo/pins/notes never become persisted data.
            operation = dict(type='rhythm_structure',
                             operations=deepcopy(active['draft']['operations']),
                             scope=deepcopy(active['draft']['scope']),
                             preserve_unedited_positions=active['initial_preserve'])
            result = editing.edit(project, active['base_revision'], operation)
            self.clear()
            return dict(ok=True, applied=True, revision=result['revision'],
                        summary=result.get('summary'), source_seconds_unchanged=True)
        raise ValueError('未知结构草稿操作。')
