"""Process-local session retention and compatibility views, not an edit reducer."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
from threading import RLock, Lock

from . import editing, editor_checkpoint as cp


_owners = {}
_registry, _opening = Lock(), Lock()


def key(project):
    # Desktop ingress resolves paths once. No stat/resolve on each gesture.
    return os.path.normcase(os.path.abspath(os.fspath(project)))


def retained(project):
    with _registry:
        return _owners.get(key(project))


def owners():
    with _registry:
        return tuple(_owners.values())


def record_document(project, record):
    doc = json.loads(record.body.decode('utf-8-sig'))
    if not isinstance(doc, dict) or doc.get('revision') != record.index['revision']:
        raise cp.CheckpointError('工作正文与清单的版本不一致。')
    if record.legacy or record.index.get('imported_legacy'):
        return editing._revision_view(Path(project) / 'editing', record.index, doc)
    return doc


def view(doc, head, marks):
    revision = head['revision']
    doc.update(range=deepcopy(head['range']), can_undo=bool(head.get('can_undo', head.get('undo'))),
               can_redo=bool(head.get('can_redo', head.get('redo'))),
               exported_revision=marks['exported_revision'], dirty=marks['exported_revision'] != revision,
               score_stale=marks['score_revision'] != revision)
    if 'selection_range' in head:
        doc['selection_range'] = deepcopy(head['selection_range'])
    else:
        doc.pop('selection_range', None)
    return doc


def _open_session(project, record):
    from . import document_editor as nd
    api = nd._api()
    idx = {k: record.index[k] for k in ('revision', 'range', 'instrument_state') if k in record.index}
    sequence = record.index.get('applied_sequence', 0)
    if record.history is not None:
        session = nd.Session(api, project, None, idx, body=record.body, history=record.history, sequence=sequence)
        try:
            head = session.head()
            if any(head[k] != record.index[k] for k in ('can_undo', 'can_redo')):
                raise cp.CheckpointError('Checkpoint history does not match its manifest')
        except BaseException:
            session.close()
            raise
        return session
    undo, redo = cp.legacy_stacks(record.index)
    legacy = record.legacy or record.index.get('imported_legacy')
    if not undo and not redo:
        return nd.Session(api, project, record_document(project, record) if legacy else None, idx,
                          body=None if legacy else record.body, sequence=sequence)
    folder = Path(project) / 'editing'
    idx.update(undo=undo, redo=redo)
    timeline = [*undo, idx['revision'], *reversed(redo)]

    def load_revision(rev):
        if rev == idx['revision']:
            return record_document(project, record)
        return editing._revision_view(folder, record.index, editing._read_revision(folder, rev))

    session = None
    try:
        session = nd.Session(api, project, load_revision(timeline[0]), idx,
                             sequence=sequence, legacy_history=True)
        for rev in timeline[1:]:
            session.append_legacy(load_revision(rev))
        return session
    except BaseException as exc:
        if session is not None:
            session.close()
        if isinstance(exc, (OSError, ValueError, TypeError, KeyError)):
            raise cp.CheckpointError('Legacy history could not be imported; nothing was overwritten.') from exc
        raise


def _verified_head(session, record):
    head = session.head()
    if (('instrument_state' in head) != ('instrument_state' in record.index)
            or head.get('instrument_state') != record.index.get('instrument_state')):
        raise cp.CheckpointError('原生版本与工程音色清单不匹配。')
    return head


def _validate_recovery(project, record):
    session = _open_session(project, record)
    try:
        _verified_head(session, record)
    finally:
        session.close()


@dataclass(frozen=True)
class _PreparedInstrument:
    fields: tuple
    state: bytes


def prepare_instrument(value):
    """Validate and hash the immutable capture on the worker, before Qt receives it."""
    ref, state = _instrument_value(value)
    return _PreparedInstrument(tuple(ref.items()), state) if ref is not None else None


def _instrument_value(value):
    if isinstance(value, _PreparedInstrument):
        return dict(value.fields), value.state
    if value is None:
        return None, None
    if not isinstance(value, dict) or value.keys() - {'instrument', 'class_id', 'state', 'sha256', 'version', 'size'}:
        raise cp.CheckpointError('无效的工程音色捕获。')
    state = value.get('state')
    if type(state) is not bytes or not 0 < len(state) <= cp._MAX_INSTRUMENT_BYTES:
        raise cp.CheckpointError('无效的工程音色大小。')
    digest = cp._digest(state)
    ref = dict(version=1, instrument=value.get('instrument'), class_id=value.get('class_id'),
               size=len(state), sha256=digest)
    if (value.get('sha256', digest) != digest or value.get('size', len(state)) != len(state)
            or type(value.get('version', 1)) is not int or value.get('version', 1) != 1):
        raise cp.CheckpointError('工程音色捕获校验失败。')
    cp.instrument_reference(ref)
    return ref, state


@dataclass(frozen=True)
class _AttachmentSnapshot:
    """One frozen native head plus its immutable opaque attachment."""
    _snapshot: object
    instrument: bytes | None

    @property
    def head(self):
        return self._snapshot.head

    def body_bytes(self):
        return self._snapshot.body_bytes()

    def history_bytes(self):
        return self._snapshot.history_bytes()


class Owner:
    def __init__(self, project, record):
        self.project, self.lock, self.closed = project, RLock(), False
        self._discarding, self._discard_stamp = False, None
        self.session = _open_session(project, record)
        try:
            self.head = _verified_head(self.session, record)
        except Exception:
            self.session.close()
            raise
        self._instrument = record.instrument
        self._instrument_route = self._instrument_baseline = None
        self._instrument_route_revision = 0
        self._instrument_seen = self._instrument_captured = -1
        self._instrument_pending = set()
        self._instrument_capture_unverified = False
        self._instrument_capture_revision = 0
        self.marks = cp.export_fields(project, record.index)
        self.export_defaults = {k:record.index[k] for k in ('exported_revision','score_revision') if k in record.index}
        self.saves = cp.Saves(project, record)

    def check(self, base=None, expected=None):
        from . import document_editor as nd
        if self.closed:
            raise nd.NativeError(nd.CLOSED, '编辑会话已关闭。')
        if ((base is not None and self.head['revision'] != base) or
                (expected is not None and tuple(expected) != tuple(self.head['stamp']))):
            raise nd.NativeError(nd.STALE, '工程编辑状态已变化；旧操作未覆盖新内容。')

    def packet(self, mode, base, op=None, *, trusted=None, expected=None, request=None, **fields):
        from . import document_editor as nd
        with self.lock:
            self.check(expected=expected)
            if isinstance(op, dict) and op.get('type') == 'instrument_state':
                raise ValueError('工程音色引用只能由工程乐器捕获设置。')
            if self._discarding and mode in (nd.APPLY, nd.DRAFT):
                raise ValueError('正在确认放弃此次编辑。')
            token = self.session.token(expected, request, mutate=mode in (nd.APPLY, nd.DRAFT))
            data = dict(base_revision=base, **fields)
            if op is not None:
                data['op'] = op
            if mode == nd.APPLY and op.get('type') not in ('range','history'):
                data['proposed_revision'] = editing.uuid4().hex
            try:
                out = self.session.run(mode, token, data, trusted)
            except nd.AppliedReplyError as exc:
                # This is a delivery error, never a rejection of the command.
                self.head = self.session.head()
                raise nd.CommittedEditError(deepcopy(self.head), exc) from exc
            if mode == nd.APPLY:
                self.head = deepcopy(out['head'])
                out['revision'] = self.head['revision']
            elif mode == nd.PREVIEW:
                out['stamp'] = list(self.head['stamp'])
            return out

    def freeze(self, expected=None, *, base=None):
        with self.lock:
            self.check(base, expected)
            return self.session.freeze(deepcopy(self.head))

    def snapshot_view(self, frozen=None):
        with self.lock:
            self.check()
            snapshot = frozen if frozen is not None else self.freeze()
            marks = dict(self.marks)
        return view(snapshot.document(), snapshot.head, marks)

    def _close_stamp(self):
        return (tuple(self.head['stamp']), self._instrument_route_revision, self._instrument_seen,
                self._instrument_capture_revision)

    def _instrument_uncaptured(self):
        return self._instrument_capture_unverified or self._instrument_seen > self._instrument_captured

    @staticmethod
    def _instrument_generation(generation):
        if type(generation) is not int or not 0 <= generation < 2**64:
            raise ValueError('无效的工程音色版本。')
        return generation

    def bind_instrument_route(self, token, generation=0, *, changed=False, baseline=None):
        if token is None:
            raise ValueError('无效的工程音色会话。')
        generation = self._instrument_generation(generation)
        ref = _instrument_value(baseline)[0] if baseline is not None else None
        with self.lock:
            self.check()
            if self._discarding or self._discard_stamp == self._close_stamp():
                raise ValueError('此版本已确认放弃，不能绑定工程音色。')
            if self._instrument_route == token:
                self._instrument_seen = max(self._instrument_seen, generation)
                return
            self._instrument_route = token
            self._instrument_route_revision += 1
            pending = self._instrument_uncaptured()
            self._instrument_seen = generation
            self._instrument_captured = -1 if changed or pending else generation
            self._instrument_baseline = ref if not changed and 'instrument_state' not in self.head else None

    def mark_instrument_changed(self, token, generation):
        generation = self._instrument_generation(generation)
        with self.lock:
            if (token is None or token != self._instrument_route or self.closed or self._discarding
                    or self._discard_stamp == self._close_stamp()):
                return False
            self._instrument_seen = max(self._instrument_seen, generation)
            return True

    def instrument_capture_failed(self, token):
        """Keep an unverifiable plug-in dirty without inventing a worker generation."""
        with self.lock:
            if (token is None or token != self._instrument_route or self.closed or self._discarding
                    or self._discard_stamp == self._close_stamp()):
                return False
            if not self._instrument_capture_unverified:
                self._instrument_capture_unverified = True
                self._instrument_capture_revision += 1
            return True

    def _check_instrument_route(self, token):
        self.check()
        if self._discarding or self._discard_stamp == self._close_stamp():
            raise ValueError('此版本已确认放弃，未更新工程音色。')
        if token != self._instrument_route:
            raise ValueError('工程乐器已变化，未使用旧音色捕获。')

    def begin_instrument_capture(self, token, generation):
        generation = self._instrument_generation(generation)
        with self.lock:
            self._check_instrument_route(token)
            if token is None:
                raise ValueError('工程乐器会话尚未绑定。')
            self._instrument_seen = max(self._instrument_seen, generation)
            ticket = object()
            self._instrument_pending.add(ticket)
            return ticket

    def end_instrument_capture(self, ticket):
        with self.lock:
            self._instrument_pending.discard(ticket)

    def instrument_state(self):
        """Opaque. A missing field is legacy; explicit null never falls back."""
        with self.lock:
            self.check()
            if 'instrument_state' not in self.head:
                return None
            ref = self.head['instrument_state']
            return dict(ref, state=self._instrument) if ref is not None else {'disabled': True}

    def _apply_instrument(self, ref, state, token, generation, *, prefer_publish=False):
        from . import document_editor as nd
        self._check_instrument_route(token)
        if token is not None:
            generation = self._instrument_generation(generation)
            if generation < self._instrument_captured:
                raise ValueError('捕获的工程音色早于已应用的音色，未覆盖。')
        elif generation is not None:
            raise ValueError('工程乐器会话尚未绑定。')
        old = self.head
        known = 'instrument_state' in old
        same = known and old['instrument_state'] == ref
        legacy_same = (not known and ref is not None and ref == self._instrument_baseline
                       and not prefer_publish)
        if not (same or legacy_same):
            token_native = self.session.token(mutate=True)
            data = dict(base_revision=old['revision'], op=dict(type='instrument_state', state=ref))
            try:
                result = self.session.run(nd.APPLY, token_native, data)
                self.head = deepcopy(result['head'])
            except nd.AppliedReplyError:
                # Terminal state is recoverable from the exact same owner.
                self.head = self.session.head()
                if self.head.get('instrument_state') != ref or 'instrument_state' not in self.head:
                    raise
            if ('instrument_state' not in self.head or self.head['instrument_state'] != ref
                    or self.head['revision'] != old['revision']):
                raise cp.CheckpointError('原生版本未确认工程音色附件。')
            self._instrument = state
        if generation is not None:
            self._instrument_seen = max(self._instrument_seen, generation)
            self._instrument_captured = generation
        if self._instrument_capture_unverified:
            self._instrument_capture_unverified = False
            self._instrument_capture_revision += 1
        return self.head['applied_sequence']

    def update_instrument_state(self, value, *, route_token=None, generation=None):
        ref, state = _instrument_value(value)
        with self.lock:
            return self._apply_instrument(ref, state, route_token, generation)

    def _save_snapshot(self):
        frozen = self.session.freeze(deepcopy(self.head), checkpoint=True)
        return _AttachmentSnapshot(frozen, self._instrument)

    def _content_dirty_parts(self):
        saved_rev, saved_inst = self.saves.formal_saved_content()
        musical = self.head['revision'] != saved_rev
        cur_inst = self.head.get('instrument_state', cp.Saves._UNSET)
        instrument = self._instrument_uncaptured() or cur_inst != saved_inst
        unconfirmed = bool(self.saves.writer.possible)
        return musical, instrument, unconfirmed

    def save_with_instrument(self, value, *, route_token=None, generation=None, retry=False, kind='formal'):
        if kind not in ('formal', 'recovery'):
            raise ValueError('无效的工程音色保存请求。')
        if retry:
            # Retry the exact already captured job, not the current plug-in.
            return self.save(retry=True) if kind == 'formal' else self.backup(retry=True)
        ref, state = _instrument_value(value)
        with self.lock:
            self._check_instrument_route(route_token)
            status = self.saves.status()
            if kind == 'recovery' and not status['recovery_enabled']:
                return dict(status='disabled')
            # Do not create a recovery edit merely by comparing an unchanged
            # legacy sidecar, but include it when saving actual unsaved edits.
            musical, instrument, unconfirmed = self._content_dirty_parts()
            migrate = kind == 'formal' or musical or instrument or unconfirmed
            self._apply_instrument(ref, state, route_token, generation, prefer_publish=migrate)
            musical, instrument, unconfirmed = self._content_dirty_parts()
            if kind == 'recovery' and (not (musical or instrument or unconfirmed)
                                       or not self.saves.due(self.head['applied_sequence'])):
                return dict(status='skipped')
            return self.saves.enqueue(self._save_snapshot(), kind=kind)

    def status(self):
        with self.lock:
            state = self.saves.status()
            uncaptured = self._instrument_uncaptured()
            musical, instrument, unconfirmed = self._content_dirty_parts()
            dirty = musical or instrument or unconfirmed
            return dict(state, busy=state['busy'] or bool(self._instrument_pending),
                        instrument_generation=self._instrument_seen, instrument_route_revision=self._instrument_route_revision,
                        instrument_capture_pending=len(self._instrument_pending),
                        instrument_capture_unverified=self._instrument_capture_unverified,
                        instrument_capture_revision=self._instrument_capture_revision,
                        instrument_discarded=self._discard_stamp == self._close_stamp(),
                        project_id=self.project, epoch=self.head['stamp'][0],
                        revision=self.head['revision'], applied_sequence=self.head['applied_sequence'],
                        save_dirty=dirty,
                        has_musical_changes=musical,
                        has_instrument_changes=instrument,
                        sequence_ahead=self.head['applied_sequence'] != state['formal_saved_sequence'],
                        recovery_touched=bool(self.saves.writer.recovery_touched),
                        recovery_current=not uncaptured and self.head['applied_sequence'] == state['recovery_saved_sequence'],
                        exported_revision=self.marks['exported_revision'],
                        history_scope='project', history_limit=None)

    def save(self, *, retry=False):
        with self.lock:
            self.check()
            if self._discarding:
                raise ValueError('正在确认放弃此次编辑。')
            if not retry and (self._instrument_uncaptured() or self._instrument_pending):
                raise ValueError('请先完成工程音色捕获；正式文件未替换。')
            return self.saves.enqueue(None if retry else self._save_snapshot(), retry=retry)

    def backup(self, *, retry=False, keep_last_instrument=False):
        with self.lock:
            self.check()
            if self._discarding or self._discard_stamp == self._close_stamp():
                return dict(status='skipped')
            if not self.saves.status()['recovery_enabled']:
                return dict(status='disabled')
            # Automatic recovery may snapshot current notes with the last
            # confirmed attachment while playback owns the vendor processor.
            # The uncaptured generation stays dirty until a real capture.
            confirmed = (keep_last_instrument and bool(self.head.get('instrument_state'))
                         and self._instrument is not None)
            musical, instrument, unconfirmed = self._content_dirty_parts()
            if not retry and (self._instrument_pending
                              or (self._instrument_uncaptured() and not confirmed)
                              or not (musical or instrument or unconfirmed)
                              or not self.saves.due(self.head['applied_sequence'])):
                return dict(status='skipped')
            return self.saves.enqueue(None if retry else self._save_snapshot(), retry=retry, kind='recovery')

    def prepare_discard(self):
        """Only for an explicit Discard after the close owner quiesces this session."""
        with self.lock:
            self.check()
            if self._discarding or self.status()['busy']:
                raise ValueError('请等待已开始的保存结束，再确认放弃。')
            stamp, sequence = self._close_stamp(), self.head['applied_sequence']
            if self._discard_stamp == stamp:
                return
            self._discarding = True
        confirmed = False
        try:
            self.saves.writer.discard_through(sequence)
            confirmed = True
        finally:
            with self.lock:
                if confirmed:
                    self._discard_stamp = stamp
                self._discarding = False

    def refresh_marks(self):
        marks = cp.export_fields(self.project, self.export_defaults)
        with self.lock:
            self.marks = marks

    def export_input(self, base, expected):
        with self.lock:
            frozen = self.freeze(expected, base=base)
            guard = self.saves.writer.export_guard()
            marks = dict(self.marks)
        return dict(project=self.project, head=frozen.head, guard=guard,
                    document=view(frozen.document(), frozen.head, marks))


def owner(project):
    from . import document_editor as nd
    k = key(project)
    old = retained(k)
    if old is not None:
        return old
    with _opening:
        old = retained(k)
        if old is not None:
            return old
        record = cp.read(k)
        if record is None:
            raise nd.NativeError(nd.MISSING, '工程尚无可编辑正文。')
        from .editor_recovery import check_open
        check_open(k, record)
        entry = Owner(k, record)
        with _registry:
            _owners[k] = entry
        return entry


def _evict(entry):
    entry.closed = True
    with _registry:
        _owners.pop(entry.project, None)
    entry.session.close()


def _release(project, *, discard=False, expected_owner=None):
    """No disk I/O. Dirty eviction requires a confirmed, exact-head discard."""
    with _opening:
        entry = retained(project)
        if entry is None:
            return True
        if expected_owner is not None and entry is not expected_owner:
            return False
        with entry.lock:
            status = entry.status()
            approved = discard and entry._discard_stamp == entry._close_stamp()
            needs_discard = status['save_dirty'] or (status['recovery_touched'] and status['sequence_ahead'])
            if entry._discarding or status['busy'] or (needs_discard and not approved):
                return False
            _evict(entry)
        return True


def release_clean(project, *, expected_owner=None):
    return _release(project, expected_owner=expected_owner)


def reopen_clean(project, *, expected_owner=None):
    if not release_clean(project, expected_owner=expected_owner):
        raise cp.CheckpointError('Owner changed, is saving, or has unsaved work.')
    return owner(project)


def release(project, *, discard=False):
    return _release(project, discard=discard)


def restore_archive(project, identity):
    # Archive identifier must refer to a currently offered, identity-bound recovery.
    offer = recovery_options(project, include_protected=True)
    row = next((r for r in (offer or {}).get('options', []) if r['digest'] == identity or r.get('archive_identity') == identity), None)
    if row is None:
        raise cp.CheckpointError('Archive is not an identity-verified recovery candidate.')
    return restore_recovery(project, offer, row['id'])




def recovery_options(project, *, include_protected=False):
    from . import editor_recovery as recovery
    project = key(project)
    with editing.edit_lock(project):
        return recovery.options_locked(project, lambda record: _validate_recovery(project, record),
                                       include_protected=include_protected)


def restore_recovery(project, offer, identifier):
    """Explicit lifecycle operation. Never displace a dirty or saving session."""
    from . import editor_recovery as recovery
    project = key(project)
    with editing.edit_lock(project), _opening:
        entry = retained(project)
        with entry.lock if entry else nullcontext():
            if entry and (entry._discarding or entry.status()['busy'] or entry.status()['save_dirty']):
                raise ValueError('当前项目有未保存的编辑或正在保存，尚未恢复副本。')
            try:
                revision = recovery.restore_locked(project, offer, identifier,
                                                   lambda record: _validate_recovery(project, record))
            except recovery.PublicationError as exc:
                if entry and exc.outcome != 'not_published':
                    _evict(entry)  # do not retain a clean view of a possibly replaced head
                raise
            if entry:
                _evict(entry)
            return revision


def restore_previous(project):
    """Compatibility entry point for an explicit, verified previous-save recovery."""
    offer = recovery_options(project)
    if not offer or not any(row['kind'] == 'previous' for row in offer['options']):
        raise cp.CheckpointError('没有适用且已核对项目身份的前次保存。')
    return restore_recovery(project, offer, 'previous')


def dismiss_recovery(project, offer):
    """Record explicit dismissal of offered auto-recovery backups without touching formal files."""
    from . import editor_recovery as recovery
    project = key(project)
    with editing.edit_lock(project):
        return recovery.dismiss_locked(project, offer)


def export_saved_legacy(project, destination):
    """Export a saved head and both stacks as a legacy editing tree in a NEW directory.

    Does not copy project.json, media, caches, or newer unsaved memory edits.
    Publish the legacy index last. Never overwrite an existing destination.
    """
    project = key(project)
    record = cp.read(project)
    if record is None:
        raise cp.CheckpointError('No saved document to export')
    session = _open_session(project, record)
    try:
        frozen = session.freeze(checkpoint=True)
    finally:
        session.close()
    head = frozen.head
    root = Path(destination)
    root.mkdir()
    folder = root / 'editing'
    (folder / 'revisions').mkdir(parents=True)

    def write_body(body, revision=None):
        doc = json.loads(body)
        label = doc['revision']
        if revision is not None and label != revision:
            raise cp.CheckpointError('Legacy export revision mismatch')
        target = editing._revision_file(folder, label)
        if target.exists() or target.is_symlink():
            raise cp.CheckpointError('Colliding revision filename in legacy export')
        cp.atomic(target, body)
        return label

    index = dict(revision=write_body(frozen.body_bytes(), head['revision']), range=head['range'])
    for side, name in enumerate(('undo', 'redo')):
        index[name] = [write_body(frozen.history_body_bytes(side, i)) for i in range(head[name + '_count'])]
    marks = cp.export_fields(project, record.index)
    index.update(marks)
    cp.atomic(folder / 'current.json', cp._encode(index))
    return index['revision']


def save(project, *, retry=False, wait=False):
    entry = owner(project)
    job = entry.save(retry=retry)
    if wait:
        status = entry.saves.wait('formal')
        if status['formal_saved_sequence'] < job['sequence']:
            raise OSError(status['error'] or '未确认保存。')
    return job


def edit_ticket(project, base):
    return query(project, base, 'info')['stamp']


def binding(project, base):
    entry = owner(project)
    frozen = entry.freeze(base=base)
    return entry.snapshot_view(frozen), list(frozen.head['stamp'])


def preview(project, base, op, *, expected=None, trusted=None):
    from .document_editor import PREVIEW
    return owner(project).packet(PREVIEW, base, op, expected=expected, trusted=trusted)


def query(project, base, name, args=None, *, expected=None):
    from .document_editor import QUERY
    return owner(project).packet(QUERY, base, expected=expected, query=name, args=args or {})


def apply(project, base, op, *, trusted=None, expected=None, request=None):
    from .document_editor import APPLY
    return owner(project).packet(APPLY, base, op, trusted=trusted, expected=expected, request=request)


def mutate(project, base, op, *, trusted=None, expected=None, request=None):
    # Compatibility entry point for callers that explicitly need a whole view.
    # The Qt gesture lane calls apply(), not mutate().
    from .document_editor import CommittedEditError
    entry = owner(project)
    with entry.lock:
        result = apply(project, base, op, trusted=trusted, expected=expected, request=request)
        try:
            snapshot = entry.freeze(result['head']['stamp'])
        except Exception as exc:
            raise CommittedEditError(result['head'], exc) from exc
    try:
        result['doc'] = entry.snapshot_view(snapshot)
    except Exception as exc:
        raise CommittedEditError(result['head'], exc) from exc
    return result


def preview_locked(project, path, index, op, **kw):
    return preview(project, index['revision'], op, **kw)


def query_locked(project, path, index, name, args=None, **kw):
    return query(project, index['revision'], name, args, **kw)


def commit_locked(project, path, index, op, **kw):
    return mutate(project, index['revision'], op, **kw)


def commit_reviewed(project, base, operation, *, expected=None):
    if operation.get('type') == 'bar_phase_candidate':
        return mutate(project, base, {'type':'bar_phase_candidate'}, expected=expected,
                      trusted={'bar_phase':operation})['doc']
    return editing.edit(project, base, operation, expected=expected)


def playback_snapshot(project, *, expected=None):
    """Acquire, then call .playback() off the audio callback. No file or Qt object."""
    return owner(project).freeze(expected)
