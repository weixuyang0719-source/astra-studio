"""Native document owner; Python owns files, authorization and compatibility views."""
from collections import OrderedDict
from copy import deepcopy
import ctypes as C
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
from threading import Lock

from . import editing
from .native_editor import _library_name

ABI = 2
OK, INVALID, STALE, DUPLICATE, BUSY, CLOSED, MISSING, LIMIT, NOMEM, INTERNAL = range(10)
QUERY, PREVIEW, PREPARE, DRAFT, APPLY = range(5)
_LOG = logging.getLogger(__name__)


class NativeError(ValueError):
    def __init__(self, status, message=None):
        self.status = status
        super().__init__(message or f'Astra document status {status}')


class CommittedEditError(OSError):
    """The memory head advanced. Refresh the view; do not retry the command."""
    def __init__(self, index, cause):
        self.index = index
        self.revision = index['revision']
        super().__init__('编辑已应用，显示更新异常（不是保存确认）：' + (str(cause) or type(cause).__name__))


class IndeterminateHeadError(OSError):
    """An attempted publication could not be classified under the lock."""


class AppliedReplyError(RuntimeError):
    """The native apply returned success but its owned reply did not decode."""


class _Span(C.Structure):
    _fields_ = [('data', C.c_char_p), ('count', C.c_size_t)]


class _Buffer(C.Structure):
    _fields_ = [('data', C.c_void_p), ('count', C.c_size_t)]


class _Stamp(C.Structure):
    _fields_ = [('epoch', C.c_uint64), ('sequence', C.c_uint64)]


class _Token(C.Structure):
    _fields_ = [('base', _Stamp), ('request', C.c_uint64)]


def _span(data):
    return _Span(data, len(data))


def _json(obj):
    return _span(json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))


class _Api:
    def __init__(self, path):
        self.dll = dll = C.CDLL(str(path))
        p, h, u, i = C.POINTER, C.c_void_p, C.c_uint32, C.c_int32
        out = p(_Buffer)
        bindings = {
            'version': (u, []),
            'seed_v1': (i, [_Span, out]),
            'timing_query_v1': (i, [_Span, out]),
            'open_v1': (i, [_Span, p(h), out]),
            'append_legacy_v2': (i, [h, _Span, out]),
            'stamp_v1': (i, [h, p(_Stamp), out]),
            'run_v1': (i, [h, p(_Token), u, _Span, _Span, out]),
            'export_v1': (i, [h, p(_Token), out]),
            'finish_v1': (i, [h, p(_Token), u, _Span, out]),
            'head_v2': (i, [h, out]),
            'capture_v2': (i, [h, p(h), out]),
            'capture_checkpoint_v2': (i, [h, p(h), out]),
            'read_snapshot_v2': (i, [h, u, out]),
            'read_history_body_v2': (i, [h, u, C.c_size_t, out]),
            'free_snapshot': (None, [h]),
            'close_v1': (i, [h]),
            'free_session': (None, [h]),
            'free_buffer': (None, [h]),
        }
        for name, (result, args) in bindings.items():
            f = getattr(dll, 'aed_' + name)
            f.restype, f.argtypes = result, args
            setattr(self, name, f)
        if self.version() != ABI:
            raise ValueError('unsupported Astra document ABI')

    def raw(self, func, *args, applied=False):
        out = _Buffer()
        try:
            rc = func(*args, C.byref(out))
            try:
                data = C.string_at(out.data,out.count) if out.count else b''
            except Exception as exc:
                if not rc and applied:
                    raise AppliedReplyError('无法复制编辑回执。') from exc
                raise
            if rc:
                detail = json.loads(data) if data else None
                raise NativeError(rc, detail.get('error') if isinstance(detail, dict) else None)
            return data
        finally:
            self.free_buffer(out.data)

    def call(self, func, *args):
        data = self.raw(func,*args)
        return json.loads(data) if data else None


@lru_cache(maxsize=4)
def _load_api(explicit):
    here = Path(__file__).resolve()
    paths = ([Path(explicit).expanduser()] if explicit else
             [here.parent / _library_name(), here.parents[2] / 'native' / 'editor_core' / _library_name()])
    errors = []
    for path in paths:
        if not explicit and not path.is_file():
            continue
        try:
            return _Api(path.resolve())
        except (OSError, ValueError, AttributeError) as exc:
            errors.append(str(exc))
    raise ValueError('缺少可用的 Astra C++ 编辑器（Document ABI 2）。' + (' ' + errors[-1] if errors else ''))


def _api():
    return _load_api(os.environ.get('ASTRA_EDITOR_CORE_LIBRARY', ''))


def _ppq():
    from .midi import PPQ
    return PPQ


def timing_projection(name, args):
    """Allowlisted native clock and export previews, without a document owner."""
    api = _api()
    return api.call(api.timing_query_v1, _json(dict(query=name, args=args)))


def semantic_operation(command, *, axis=None, cursor_seconds=None, clipboard=None):
    """Build an explicit view envelope. Copy's semantic_preview.clipboard can be
    fed unchanged into paste here. No hidden clipboard mutation in preview.
    Trusted detection/settle data is supplied separately by the host.
    """
    view = {}
    for key, value in (("axis", axis), ("cursor_seconds", cursor_seconds), ("clipboard", clipboard)):
        if value is not None:
            view[key] = deepcopy(value)
    return dict(type="note_semantic", command=deepcopy(command), context=view)


def validate(doc):
    api = _api()
    return api.call(api.seed_v1, _json(dict(ppq=_ppq(), validate=doc)))['duration']


def initialize(**source):
    api = _api()
    return api.call(api.seed_v1, _json(dict(ppq=_ppq(), initialize=source)))


class Frozen:
    def __init__(self, api, head):
        self.api, self.handle, self._head = api, C.c_void_p(), head

    @property
    def head(self):
        return deepcopy(self._head)

    def __del__(self):
        if self.handle:
            self.api.free_snapshot(self.handle)

    def body_bytes(self):
        return self.api.raw(self.api.read_snapshot_v2,self.handle,0)

    def history_bytes(self):
        return self.api.raw(self.api.read_snapshot_v2, self.handle, 2)

    def history_body_bytes(self, side, index):
        return self.api.raw(self.api.read_history_body_v2, self.handle, side, index)

    def document(self):
        return json.loads(self.body_bytes())

    def playback(self):
        return self.api.call(self.api.read_snapshot_v2,self.handle,1)


class Session:
    def __init__(self, api, project_id, document, index, *, body=None, sequence=0,
                 history=None, legacy_history=False):
        self.api, self.handle, self.counter = api, C.c_void_p(), 0
        args = dict(ppq=_ppq(),project_id=str(project_id),index=index,saved_sequence=sequence)
        if legacy_history:
            args['legacy_history'] = True
        if body is None:
            if history is not None:
                raise ValueError('Compact history requires a checkpoint body')
            packet = _json(dict(args,document=document))
        else:
            prefix = json.dumps(args,separators=(',',':'),allow_nan=False).encode('utf-8')
            suffix = b',"history":'+history if history is not None else b''
            packet = _span(prefix[:-1]+b',"document":'+body+suffix+b'}')
        api.call(api.open_v1,packet,C.byref(self.handle))

    def append_legacy(self, document):
        self.api.call(self.api.append_legacy_v2, self.handle, _json(document))

    def head(self):
        return self.api.call(self.api.head_v2,self.handle)

    def freeze(self, head=None, *, checkpoint=False):
        head = self.head() if head is None else head
        if checkpoint and 'selection_range' in head:
            head = {key: value for key, value in head.items() if key != 'selection_range'}
        snapshot = Frozen(self.api,head)
        capture = self.api.capture_checkpoint_v2 if checkpoint else self.api.capture_v2
        self.api.call(capture,self.handle,C.byref(snapshot.handle))
        return snapshot

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            self.api.close_v1(handle)
            self.api.free_session(handle)

    def __del__(self):
        self.close()

    def stamp(self):
        s = _Stamp()
        self.api.call(self.api.stamp_v1, self.handle, C.byref(s))
        return s.epoch, s.sequence

    def token(self, expected=None, request=None, *, mutate=False):
        stamp = self.stamp() if expected is None else tuple(expected)
        if len(stamp) != 2 or any(type(n) is not int or not 0 <= n < 2**64 for n in stamp):
            raise NativeError(INVALID, '无效的编辑器版本令牌。')
        if mutate:
            number = self.counter + 1 if request is None else request
            if type(number) is not int or not 0 < number < 2**64:
                raise NativeError(LIMIT, '无效的编辑请求编号。')
            self.counter = max(self.counter, number)
        else:
            number = 0
        return _Token(_Stamp(*stamp), number)

    def run(self, mode, token, packet, trusted=None):
        raw = self.api.raw(self.api.run_v1,self.handle,C.byref(token),mode,
                           _json(packet),_Span() if trusted is None else _json(trusted),applied=mode==APPLY)
        try:
            return json.loads(raw)
        except Exception as exc:
            if mode==APPLY:
                raise AppliedReplyError('无法解码已应用的编辑回执。') from exc
            raise

    def export(self, token):
        return self.api.call(self.api.export_v1, self.handle, C.byref(token))

    def finish(self, token, accept, observed=b''):
        self.api.call(self.api.finish_v1, self.handle, C.byref(token), int(accept), _span(observed))


def detached(doc):
    """Worker-only applicability checks on an already owned immutable input."""
    return Session(_api(), 'detached musical validation', doc,
                   dict(revision=doc['revision'], undo=[], redo=[], range=doc.get('range')))


# Public compatibility facade. The live owner never calls the disk revision publisher.
from .live_owner import (apply, binding, commit_locked, commit_reviewed, edit_ticket,
                         export_saved_legacy, mutate, owner, playback_snapshot, preview, preview_locked,
                         query, query_locked, release, release_clean, reopen_clean, restore_archive,
                         restore_previous, save)


def preview_rhythm_inspection(session, doc, tempo, identity):
    return session.run(PREVIEW, session.token(), dict(base_revision=doc['revision'],
                       op=dict(type='adopt_rhythm_candidate')),
                       dict(rhythm_inspection=dict(tempo=tempo, identity=identity)))


class StructureDraftSession:
    """Compatibility response and a recovery mirror, never a Python reducer."""
    def __init__(self):
        self._active = None

    def pending(self, project):
        a = self._active
        return bool(a and a['project'] == Path(project).resolve() and a['operation_count'])

    def clear(self):
        a = self._active
        if a is not None:
            try:
                self.handle(a['project'], dict(action='discard', token=a['token'], draft_version=a['version']))
            except (ValueError, OSError, KeyError):
                # A released/stale owner is not a reason to touch the head.
                self._active = None

    def handle(self, project, request):
        if not isinstance(request, dict):
            raise ValueError('结构编辑请求必须是对象。')
        action = request.get('action')
        project = Path(project).resolve()
        a = self._active
        if action != 'begin':
            if not a or project != a['project'] or request.get('token') != a['token']:
                raise NativeError(STALE, '结构草稿已失效，请重新进入编辑。')
            if type(request.get('draft_version')) is not int or request['draft_version'] != a['version']:
                raise NativeError(STALE, '草稿已有更新，请使用最新草稿。')
        entry = owner(project)
        with entry.lock:
            base = entry.head['revision'] if action=='discard' else request.get('base_revision')
            editing._check_base(entry.head,base)
            s = entry.session
            epoch, _ = s.stamp()
            if action != 'begin' and a['epoch'] != epoch:
                if action == 'discard':
                    self._active = None
                    return dict(ok=True, discarded=True)
                raise NativeError(STALE, '结构草稿的工程会话已失效。')
            if action == 'apply':
                try:
                    receipt = mutate(project,base,dict(type='draft_apply',request=request))
                except CommittedEditError as exc:
                    self._active = None
                    return dict(ok=True, applied=True, revision=exc.revision, warning=str(exc),
                                source_seconds_unchanged=True)
                self._active = None
                doc = receipt['doc']
                return dict(ok=True, applied=True, revision=doc['revision'], summary=doc.get('summary'),
                            source_seconds_unchanged=True)
            try:
                reply = entry.packet(DRAFT,base,request)
            except NativeError as exc:
                if action == 'discard' and exc.status == STALE:
                    self._active = None
                    return dict(ok=True, discarded=True)
                raise
            operations = reply.pop('_recovery_operations', [])
            if action == 'discard':
                self._active = None
            else:
                self._active = dict(project=project, epoch=epoch, token=reply['token'],
                                    version=reply['draft_version'], base_revision=reply['base_revision'],
                                    operation_count=reply['operation_count'],
                                    draft=dict(deepcopy(reply['draft']), operations=operations))
            return reply
