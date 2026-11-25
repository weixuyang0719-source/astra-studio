"""Independent formal and recovery clocks; jobs own frozen snapshots only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from threading import Condition
from time import time_ns
from uuid import uuid4


_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='astra-checkpoint')


class Writer:
    def __init__(self, project, record):
        self.project = os.path.normcase(os.path.abspath(project))
        self.lineage = uuid4().hex
        self.anchor = record.identity         # confirmed formal image only
        self.possible = frozenset()            # permitted but unconfirmed images
        self.recovery_touched = False

    def export_guard(self):
        return dict(anchor=self.anchor, lineage=self.lineage)

    def _snapshot(self, job):
        from .editor_checkpoint import CheckpointError
        head = job.snapshot.head
        if (head.get('project_id') != self.project or not isinstance(head.get('revision'), str)
                or type(job.sequence) is not int or not 0 <= job.sequence < 2**64):
            raise CheckpointError('保存目标与捕获的项目版本不匹配。')
        history = job.snapshot.history_bytes() if hasattr(job.snapshot, 'history_bytes') else None
        return head, job.snapshot.body_bytes(), history, getattr(job.snapshot, 'instrument', None)

    def _base(self):
        from . import editor_checkpoint as cp, editor_recovery as recovery
        old = cp.read(self.project)
        if old is None or (old.identity != self.anchor and old.identity not in self.possible):
            raise cp.CheckpointError('正式版本已移除或被其他写入者修改；内存编辑保留，未覆盖。')
        store = recovery.scope(self.project, seed=old)
        return old, store, recovery.family(store, old)

    def _index(self, job, head, old, store, family):
        from .score_commit import KEY
        fallback = {k: old.index[k] for k in ('exported_revision', 'score_revision', KEY) if k in old.index}
        if 'instrument_state' in head:
            fallback['instrument_state'] = head['instrument_state']
        return dict(fallback, revision=head['revision'], range=head['range'], applied_sequence=job.sequence,
                    checkpoint_id=job.id, checkpoint_owner=self.lineage, save_project=store.id, save_family=family,
                    can_undo=bool(head.get('can_undo', False)), can_redo=bool(head.get('can_redo', False)))

    def write(self, job, score=None):
        from contextlib import nullcontext
        from . import editor_checkpoint as cp, editor_recovery as recovery
        from .editing import edit_lock
        from .pipeline import project_lock
        from .score_commit import KEY, binding
        head, body, history, instrument = self._snapshot(job)
        with (project_lock(self.project) if score is not None else nullcontext()), edit_lock(self.project):
            old, store, family = self._base()
            index = self._index(job, head, old, store, family)
            if score is not None:
                receipt, proof = score
                if ((receipt.lineage, receipt.revision, receipt.source) !=
                        (self.lineage, head['revision'], head['source_key'])
                        or receipt.sequence > job.sequence
                        or Path(self.project).resolve() != receipt.project):
                    raise cp.CheckpointError('工作版本已变化，本次谱面未保存。')
                index[KEY] = binding(receipt, proof, old)
            path = store.editing / 'current.json'
            # Reject a redirected formal file before touching any formal names.
            prior = recovery._read(path)
            if prior != old.raw:
                raise cp.CheckpointError('正式文件已变化，未保存。')
            data = cp.pack(index, body, history=history, instrument=instrument)
            wanted = cp._digest(data)
            if old.identity != wanted:
                previous = store.editing / 'checkpoint.previous.json'
                if old.history is None:
                    cp.archive_raw(store.editing, old.raw)
                    recovery.protect_legacy_archive(store, old, family)
                prior_previous = recovery._optional(previous)
                if prior_previous is not None and not prior_previous.startswith(cp._HISTORY_PREFIXES):
                    cp.archive_raw(store.editing, prior_previous)
                try:
                    prior_index = recovery.bound_index(store, old, family)
                    data_previous = cp.pack(prior_index, old.body, history=old.history, instrument=old.instrument)
                    recovery.verified_write(previous, data_previous,
                                            cp.atomic, recovery._optional(previous))
                except (OSError, ValueError) as exc:
                    raise cp.CheckpointError('未能确认前一正式版本的保护副本，正式文件未替换。') from exc
            self.possible = frozenset((old.identity, wanted))
            try:
                recovery.verified_write(path, data, cp.atomic, prior)
            except recovery.PublicationError as exc:
                if exc.outcome == 'not_published':
                    self.possible = frozenset()
                elif score is not None:
                    if exc.outcome == 'present_unconfirmed':
                        # The single commit is visible in full. Retry only the
                        # durability flush, never a rename or the edit itself.
                        self.anchor, self.possible = wanted, frozenset()
                        try:
                            recovery._flush_existing(path,data)
                        except (OSError, ValueError):
                            return 'committed_unconfirmed_durability'
                        return None
                    try:
                        still_old = recovery._optional(path) == prior
                    except (OSError, ValueError):
                        still_old = False
                    if still_old:
                        self.possible = frozenset()
                        raise cp.CheckpointError('无法替换正式检查点；音符、拍点和原谱未改变。') from exc
                raise
            self.anchor, self.possible = wanted, frozenset()

    def backup(self, job, permit):
        from . import editor_recovery as recovery
        from .editing import edit_lock
        if permit() is None:
            return recovery.SKIPPED
        head, body, history, instrument = self._snapshot(job)
        if permit() is None:
            return recovery.SKIPPED
        with edit_lock(self.project):
            if permit() is None:
                return recovery.SKIPPED
            old, store, family = self._base()
            self.recovery_touched = True
            return recovery.write_backup(store, self._index(job, head, old, store, family), body, job, permit,
                                         history=history, instrument=instrument)

    def rotate(self, limit):
        from . import editor_recovery as recovery
        from .editing import edit_lock
        with edit_lock(self.project):
            store = recovery.scope(self.project)
            if store:
                recovery.rotate(store, limit)

    def discard_through(self, sequence):
        from . import editor_checkpoint as cp, editor_recovery as recovery
        from .editing import edit_lock
        if not self.possible and not self.recovery_touched:
            return
        with edit_lock(self.project):
            if self.possible:
                old = cp.read(self.project)
                if old is None or old.identity != self.anchor:
                    raise cp.CheckpointError('上次正式保存结果尚未确认。请先重试或检查正式文件，不能确认放弃。')
            if self.recovery_touched:
                store = recovery.scope(self.project)
                if store is None:
                    raise cp.CheckpointError('无法记录放弃决定，项目仍保留在内存中。')
                recovery.discard_through(store, self.lineage, sequence)


@dataclass(frozen=True)
class Save:
    snapshot: object
    id: str
    kind: str = 'formal'
    created_ns: int = field(default_factory=time_ns)
    gate: int = 0

    @property
    def sequence(self):
        return self.snapshot.head['applied_sequence'] if self.snapshot is not None else None


class Saves:
    """One runner, one replaceable queued capture per lane. No owner lock or Qt call in the worker."""
    _UNSET = object()

    @classmethod
    def _content_key(cls, mapping):
        inst = mapping.get('instrument_state', cls._UNSET)
        if isinstance(inst, dict):
            inst = dict(inst)
        return (mapping.get('revision'), inst)

    def __init__(self, project, record):
        self.writer = Writer(project, record)
        self.cv = Condition()
        self.active = None
        self.pending, self.failed, self.errors = {}, {}, {}
        self.saved = {'formal': (record.index.get('applied_sequence', 0), record.index['revision'],
                                  record.index.get('checkpoint_id')),
                      'recovery': (None, None, None)}
        try:
            formal_ms = (Path(self.writer.project) / 'editing' / 'current.json').stat().st_mtime_ns // 10**6
        except OSError:
            formal_ms = None
        self.saved_ms = {'formal': formal_ms, 'recovery': None}
        self.saved_content = {'formal': self._content_key(record.index), 'recovery': None}
        self.enabled, self.limit, self.gate = False, None, 0
        self.rotation_warning = None

    def formal_saved_content(self):
        with self.cv:
            return self.saved_content['formal']

    def _lane(self, kind):
        running = self.active if self.active and self.active.kind == kind else None
        queued, failed = self.pending.get(kind), self.failed.get(kind)
        seq, revision, identifier = self.saved[kind]
        error, outcome = self.errors.get(kind, (None, None))
        return dict(saved_sequence=seq, saved_revision=revision, save_id=identifier,
                    saved_ms=self.saved_ms[kind],
                    saving=running.id if running else None, saving_sequence=running.sequence if running else None,
                    queued=queued.id if queued else None, queued_sequence=queued.sequence if queued else None,
                    failed_save=failed.id if failed else None, failed_sequence=failed.sequence if failed else None,
                    error=error, outcome=outcome)

    def status(self):
        with self.cv:
            formal, recovery = self._lane('formal'), self._lane('recovery')
            recovery['warning'] = self.rotation_warning
            recovery['retention_busy'] = ('retention' in self.pending or
                self.active is not None and self.active.kind == 'retention')
            # Old names are formal-only, including for callers in the close audit.
            return dict(formal=formal, recovery=recovery, formal_saved_sequence=formal['saved_sequence'],
                        recovery_saved_sequence=recovery['saved_sequence'], recovery_enabled=self.enabled,
                        persisted_sequence=formal['saved_sequence'], persisted_revision=formal['saved_revision'],
                        checkpoint_id=formal['save_id'], busy=self.active is not None or bool(self.pending),
                        **{k: formal[k] for k in ('saving', 'saving_sequence', 'queued', 'queued_sequence',
                                                 'failed_save', 'failed_sequence', 'error')})

    def configure(self, enabled, limit):
        with self.cv:
            if self.enabled != enabled:
                self.enabled = enabled
                self.gate += 1
                if not enabled:
                    self.pending.pop('recovery', None)
            if self.limit != limit:
                self.limit = limit
                self.pending['retention'] = Save(None, uuid4().hex, 'retention')
            self._kick()

    def due(self, sequence):
        with self.cv:
            lane = self._lane('recovery')
            return (self.enabled and not lane['failed_save'] and sequence != self.saved['formal'][0]
                    and all(s is None or sequence > s for s in (lane['saved_sequence'],
                            lane['saving_sequence'], lane['queued_sequence'])))

    def capture_error(self, kind, exc):
        with self.cv:
            if kind not in self.failed:
                self.errors[kind] = (str(exc) or type(exc).__name__, 'not_published')

    def _take(self):
        for kind in ('formal', 'recovery', 'retention'):
            job = self.pending.pop(kind, None)
            if job:
                return job
        return None

    def _kick(self):
        if self.active is not None:
            return
        self.active = self._take()
        if self.active is None:
            self.cv.notify_all()
            return
        try:
            _POOL.submit(self._drain)
        except Exception as exc:
            while self.active is not None:
                self._finish(self.active, None, exc)
                self.active = self._take()
            self.cv.notify_all()

    def enqueue(self, snapshot=None, *, retry=False, kind='formal'):
        if kind == 'retention':
            if not retry or snapshot is not None:
                raise ValueError('清理仅支持重试。')
            with self.cv:
                for item in (self.pending.get(kind), self.active):
                    if item is not None and item.kind == kind:
                        return dict(status='queued', save_id=item.id, sequence=None)
                if not self.rotation_warning:
                    return dict(status='already_clean', sequence=None)
                job = Save(None, uuid4().hex, 'retention')
                self.pending['retention'] = job
                self._kick()
                return dict(status='queued', save_id=job.id, sequence=None)
        if kind not in self.saved:
            raise ValueError('未知的保存类型。')
        with self.cv:
            if kind == 'recovery' and not self.enabled:
                return dict(status='disabled')
            if retry:
                if kind not in self.failed:
                    raise ValueError('没有可重试的保存。')
                job = replace(self.failed[kind], gate=self.gate)
            else:
                job = Save(snapshot, uuid4().hex, kind, gate=self.gate)
            sequence, _, identifier = self.saved[kind]
            if sequence is not None and job.sequence <= sequence:
                return dict(status='already_saved', save_id=identifier, sequence=sequence)
            for item in (self.pending.get(kind), self.active):
                if item is not None and item.kind == kind and item.sequence >= job.sequence:
                    return dict(status='queued', save_id=item.id, sequence=item.sequence)
            self.pending[kind] = job
            self._kick()
            failed = self.failed.get(kind)
            return dict(status='failed' if self.active is None and failed is job else 'queued',
                        save_id=job.id, sequence=job.sequence)

    def write_score(self, snapshot, receipt, proof):
        # Owner thread holds its edit lock. Do not leapfrog any queued writer.
        # This accepts the same sequence as a previous Save: an unchanged Notes
        # document still receives a new score and formal checkpoint.
        with self.cv:
            if self.active is not None or self.pending:
                return 'busy'
            job = Save(snapshot, uuid4().hex, gate=self.gate)
            outcome = self.writer.write(job, score=(receipt, proof))
            self._finish(job, outcome, None)
            return outcome or 'committed'

    def _allow(self, job):
        with self.cv:
            return self.limit if self.enabled and job.gate == self.gate else None

    def _finish(self, job, result, exc):
        from .editor_recovery import SKIPPED
        kind = job.kind
        if kind == 'retention':
            self.rotation_warning = None if exc is None else (str(exc) or type(exc).__name__)
        elif exc is not None:
            self.failed[kind] = job
            self.errors[kind] = (str(exc) or type(exc).__name__, getattr(exc, 'outcome', 'not_published'))
        elif result is not SKIPPED:
            self.saved[kind] = job.sequence, job.snapshot.head['revision'], job.id
            self.saved_ms[kind] = time_ns() // 10**6
            self.saved_content[kind] = self._content_key(job.snapshot.head)
            self.failed.pop(kind, None)
            self.errors.pop(kind, None)
            if kind == 'recovery':
                self.rotation_warning = result

    def _drain(self):
        while True:
            with self.cv:
                job, limit = self.active, self.limit
            result, exc = None, None
            try:
                if job.kind == 'formal':
                    self.writer.write(job)
                elif job.kind == 'recovery':
                    result = self.writer.backup(job, lambda: self._allow(job))
                else:
                    self.writer.rotate(limit)
            except Exception as error:
                exc = error
            with self.cv:
                self._finish(job, result, exc)
                self.active = self._take()
                self.cv.notify_all()
                if self.active is None:
                    return

    def wait(self, kind=None):
        with self.cv:
            if kind is None:
                self.cv.wait_for(lambda: self.active is None and not self.pending)
            else:
                self.cv.wait_for(lambda: (self.active is None or self.active.kind != kind) and kind not in self.pending)
        return self.status()
