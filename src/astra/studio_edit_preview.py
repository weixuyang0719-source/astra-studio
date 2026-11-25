"""Latest-wins editor audition. The disk history never depends on this lane."""
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import Event
from time import perf_counter

from PySide6.QtCore import QRunnable, Slot

from . import editing
from .preview_identity import preview_cache_key

_LOG = logging.getLogger(__name__)

@dataclass(eq=False, frozen=True)
class _Ticket:
    project: str
    generation: object
    revision: str
    queued_at: object = None
    cancelled: Event = field(default_factory=Event, repr=False)

def _stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

@lru_cache(maxsize=32)
def _revision_midi(project, revision, stamp):
    # Memoize a path, not a revision body. The stamp is part of the memo key.
    # lru_cache does not hold its bookkeeping lock during the build.
    body = editing._read_revision(Path(project)/'editing', revision)
    return editing.preview_path(project, body, _owned=True)

def _midi_for_ticket(ticket):
    project = Path(ticket.project).resolve()
    folder = project/'editing'
    root = folder/'previews'
    if root.resolve() != root:
        raise ValueError('MIDI 预览目录不在当前工程内。')
    source = editing._revision_file(folder, ticket.revision)
    stamp = _stamp(source)
    args = str(project), ticket.revision, stamp
    path = _revision_midi(*args)
    if not path.is_file():  # a derived file can be deleted independently
        path = _revision_midi.__wrapped__(*args)
    path = path.resolve()
    if _stamp(source) != stamp or path.parent != root or not path.is_file():
        raise ValueError('编辑版本或 MIDI 预览文件已变化。')
    return path

def _parse_midi(path):
    from .preview_worker import prepare_preview, unpack_midi
    return unpack_midi(prepare_preview('midi', path))

class _PreviewTask(QRunnable):
    def __init__(self, ticket, cached, signal):
        super().__init__()
        self.ticket, self.cached, self.signal = ticket, cached, signal

    def run(self):
        ticket = self.ticket
        start = perf_counter() if ticket.queued_at is not None else None
        built = None
        asset, error = None, ''
        try:
            if not ticket.cancelled.is_set():
                path = _midi_for_ticket(ticket)
                if start is not None:
                    built = perf_counter()
                if not ticket.cancelled.is_set():
                    key = preview_cache_key(path)
                    if key is None:
                        raise ValueError('MIDI 预览文件不可用。')
                    # The shallow cache snapshot holds opaque prepared payloads.
                    # This worker never mutates or traverses a borrowed payload.
                    parsed = self.cached.get(key)
                    if parsed is None:
                        parsed = _parse_midi(path)
                    if preview_cache_key(path) != key:
                        raise ValueError('解析期间 MIDI 预览文件已变化。')
                    if not ticket.cancelled.is_set():
                        asset = dict(path=path, key=key, parsed=parsed)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        if start is not None:
            end = perf_counter()
            if built is None:
                built = end
            _LOG.warning('Astra preview queue=%.1fms asset=%.1fms parse/check=%.1fms superseded=%s',
                         1000*(start-ticket.queued_at), 1000*(built-start),
                         1000*(end-built), ticket.cancelled.is_set())
        # Even a cancelled task must release the logical active slot. Only the
        # receiver starts another task, and the signal owner outlives close/join.
        try:
            self.signal.preview.emit(ticket, asset, error)
        except RuntimeError:
            pass

class EditorPreviews:
    """GUI-owned scheduling: one running/ready result and one replaceable target."""

    def _edit_preview_selected(self, doc=None):
        if doc is None:
            doc = (self._project or {}).get('editor') or {}
        path = doc.get('preview_path')
        return bool(path and path == (getattr(self,'_midi_file',None) or {}).get('path'))

    def _edit_midi_status(self):
        if not self._edit_preview_selected():
            return False, None
        doc = self._project['editor']
        if self._edit_revision_pending() or doc.get('preview_pending'):
            return True, None
        error = doc.get('preview_error')
        if not error and doc.get('preview_revision') != doc['revision']:
            error = '当前编辑版本的 MIDI 预览不可用。'
        return False, error

    def _edit_preview_invalidate(self):
        latest = getattr(self, '_edit_preview_latest', None)
        if latest is not None:
            latest.cancelled.set()
        self._edit_preview_latest = self._edit_preview_waiting = self._edit_preview_ready = None
        # Leave a running task in its slot until its own completion. Also leave
        # an already posted install notification posted; it carries no asset.

    def _edit_preview_queue(self, packet, revision):
        self._edit_preview_invalidate()
        ticket = _Ticket(packet['project'], packet['generation'], revision,
                         perf_counter() if self._edit_trace else None)
        self._edit_preview_latest = self._edit_preview_waiting = ticket

    def _edit_preview_current(self, ticket):
        if self._closed or ticket is not self._edit_preview_latest:
            return False
        if ticket.generation is not self._edit_identity():
            return False
        project = self._project or {}
        doc = project.get('editor') or {}
        return str(project.get('path','')) == ticket.project and doc.get('revision') == ticket.revision

    def _edit_preview_start(self):
        # Don't launch or install while any commit could be ahead of its GUI
        # completion. In particular, equal revision IDs do not defeat the gate.
        if self._closed or self._edit_pending() or self._edit_preview_active is not None:
            return
        if self._edit_preview_ready is not None:
            if not self._edit_preview_posted:
                self._edit_preview_posted = True
                self._edit_done_signal.install_preview.emit()
            return
        ticket, self._edit_preview_waiting = self._edit_preview_waiting, None
        if ticket is None or not self._edit_preview_current(ticket):
            return
        self._edit_preview_active = ticket
        try:
            cached = dict(getattr(self, '_preview_midi_cache', {}))
            self._edit_preview_pool.start(_PreviewTask(ticket, cached, self._edit_done_signal))
        except Exception as exc:
            self._edit_preview_done(ticket, None, str(exc) or type(exc).__name__)

    @Slot(object, object, str)
    def _edit_preview_done(self, ticket, asset, error):
        if ticket is not self._edit_preview_active:
            return
        self._edit_preview_active = None
        if self._edit_preview_current(ticket):
            self._edit_preview_ready = ticket, asset, error
        self._edit_preview_start()

    @Slot()
    def _edit_preview_install(self):
        self._edit_preview_posted = False
        if self._closed or self._edit_pending():
            return
        ready, self._edit_preview_ready = self._edit_preview_ready, None
        if ready is None:
            return
        ticket, asset, error = ready
        if not self._edit_preview_current(ticket):
            self._edit_preview_start()
            return
        start = perf_counter() if self._edit_trace else None
        doc = self._project['editor']
        old_warning = doc.get('preview_error')
        try:
            if error or asset is None:
                raise ValueError(error or '未生成')
            self._edit_preview_use(ticket, doc, asset)
        except Exception as exc:
            if self._edit_preview_current(ticket):
                warning = '编辑已保存，但 MIDI 预览暂不可用：' + (str(exc) or type(exc).__name__)
                doc['preview_error'] = warning
                if self._edit_preview_selected(doc): self._cancel_midi_intent()
                if not self._error or self._error == old_warning:
                    self._error = warning
        else:
            if self._edit_preview_current(ticket):
                doc.pop('preview_error', None)
                if old_warning and self._error == old_warning:
                    self._error = None
        finally:
            if self._edit_preview_current(ticket):
                doc['preview_pending'] = False
                self._edit_preview_latest = None
                self._flush_midi_intent()
                if self._edit_preview_selected(doc): self._notify_midi()
                self._publish()
            self._edit_preview_start()
            if start is not None:
                _LOG.warning('Astra preview gui=%.1fms', 1000*(perf_counter()-start))

    def _edit_preview_use(self, ticket, doc, asset):
        path, key = asset['path'], asset['key']
        root = Path(ticket.project).resolve()/'editing'/'previews'
        if path.parent != root or path.resolve() != path or key is None or preview_cache_key(path) != key:
            raise ValueError('MIDI 预览文件已变化，未切换试听。')

        selected = self._midi_file
        owns_player = self._edit_preview_selected(doc)

        fields = ('path', 'preview_path', 'preview_revision')
        old_fields = {k:doc[k] for k in fields if k in doc}
        old_clock = getattr(self, '_editor_preview_clock', None)
        # Authorize exactly this asset for the synchronous, prepared select.
        # No structural publish until the selection succeeds or is rolled back.
        doc.update(path=str(path), preview_path=str(path), preview_revision=ticket.revision)
        self._editor_preview_clock = str(path), doc['tempo'], doc['duration']
        try:
            if owns_player:
                self._select_midi(str(path), prepared=asset)
            else:
                self._remember_parsed_midi(key, asset['parsed'])
        except Exception:
            for name in fields:
                doc.pop(name, None)
            doc.update(old_fields)
            if self._edit_preview_current(ticket):
                self._editor_preview_clock = old_clock
                if owns_player and self._midi_file is None:
                    # Keep selection identity for the error and a later Undo;
                    # do not restore a player or a failed playback request.
                    self._midi_file = dict(path=selected['path'],name=Path(selected['path']).stem,notes=[])
            raise
