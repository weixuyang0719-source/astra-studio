"""Private native projection. The editing directory, not this cache, owns history."""
from collections import OrderedDict
from copy import deepcopy
import ctypes as C
from functools import lru_cache
import logging
import math
import os
from pathlib import Path
import sys
from threading import Lock

from . import editing

ABI = 1
OK, INVALID, STALE, DUPLICATE, BUSY, CLOSED, MISSING, LIMIT, NOMEM, INTERNAL = range(10)
_BEFORE, _AFTER, _MANUAL = 1, 2, 4
_TIME, _END, _DATA1, _DATA2 = 1, 2, 4, 8
_NAMES = ('notes', 'controls')
_VIEW_FLAGS = frozenset(('can_undo', 'can_redo', 'dirty', 'score_stale', 'exported_revision'))
_STATIC_EXCEPT = _VIEW_FLAGS | frozenset((*_NAMES, 'deleted_ids'))
_MAX_KEY = (1 << 64) - 1
_LOG = logging.getLogger(__name__)
_U32, _I32, _Key, _Size, _Handle, _Double = C.c_uint32, C.c_int32, C.c_uint64, C.c_size_t, C.c_void_p, C.c_double
_P = C.POINTER


class _Bytes(C.Structure):
    _fields_ = [('data', C.c_char_p), ('count', _Size)]


class _Bounds(C.Structure):
    _fields_ = [('flags', _U32), ('opening', _Double), ('closing', _Double), ('extent', _Double)]


class _Row(C.Structure):
    _fields_ = [('key', _Key), ('order', _Key), ('tick', _Key), ('time', _Double), ('end', _Double),
                ('data1', _I32), ('data2', _I32), ('channel', _I32)]


class _Seed(C.Structure):
    _fields_ = [('size', _U32), ('abi', _U32), ('revision', _Bytes), ('bounds', _Bounds),
                ('notes', _P(_Row)), ('note_count', _Size), ('controls', _P(_Row)), ('control_count', _Size)]


class _Stamp(C.Structure):
    _fields_ = [('epoch', _Key), ('sequence', _Key)]


class _Token(C.Structure):
    _fields_ = [('base', _Stamp), ('request', _Key)]


class _Update(C.Structure):
    _fields_ = [('row', _Row), ('mask', _U32)]


class _Command(C.Structure):
    _fields_ = [('size', _U32), ('abi', _U32), ('revision', _Bytes), ('token', _Token), ('group', _U32),
                ('deletes', _P(_Key)), ('delete_count', _Size), ('updates', _P(_Update)), ('update_count', _Size),
                ('adds', _P(_Row)), ('add_count', _Size)]


class _Change(C.Structure):
    _fields_ = [('key', _Key), ('flags', _U32), ('before', _Row), ('after', _Row)]


class _Result(C.Structure):
    _fields_ = [('size', _U32), ('abi', _U32), ('token', _Token), ('group', _U32), ('changed', _U32),
                ('changes', _P(_Change)), ('change_count', _Size),
                ('normalized', _P(_Key)), ('normalized_count', _Size)]


def _revision_bytes(revision):
    data = revision.encode('utf-8', 'surrogatepass')
    return _Bytes(data, len(data))


class _NativeError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'editor core status {status}')


def _check(status):
    if status != OK:
        raise _NativeError(status)


class _Api:
    def __init__(self, path):
        # CDLL, not PyDLL: no Python API or callbacks in a native call.
        self.dll = dll = C.CDLL(str(path))
        dll.aec_version.argtypes = []
        dll.aec_version.restype = _U32
        if dll.aec_version() != ABI:
            raise ValueError('unsupported Astra editor core ABI')
        bindings = {
            'open': (_I32, [_P(_Seed), _P(_Handle)]),
            'get_stamp': (_I32, [_Handle, _P(_Stamp)]),
            'prepare': (_I32, [_Handle, _P(_Command), _P(_Handle)]),
            'get_result': (_I32, [_Handle, _P(_Result)]),
            'bind_revision': (_I32, [_Handle, _P(_Token), _Bytes]),
            'accept': (_I32, [_Handle, _P(_Token)]),
            'discard': (_I32, [_Handle, _P(_Token)]),
            'close': (_I32, [_Handle]),
            'free_session': (None, [_Handle]),
            'free_prepared': (None, [_Handle]),
        }
        for name, (result, args) in bindings.items():
            func = getattr(dll, 'aec_' + name)
            func.restype, func.argtypes = result, args
            setattr(self, name, func)


def _library_name():
    if os.name == 'nt':
        return 'astra_editor_core.dll'
    return 'libastra_editor_core' + ('.dylib' if sys.platform == 'darwin' else '.so')


@lru_cache(maxsize=4)
def _load_api(explicit):
    here = Path(__file__).resolve()
    paths = ([Path(explicit).expanduser()] if explicit else
             [here.parent / _library_name(), here.parents[2] / 'native' / 'editor_core' / _library_name()])
    for path in paths:
        try:
            if explicit or path.is_file():
                return _Api(path.resolve())
        except (OSError, ValueError, AttributeError) as exc:
            _LOG.warning('Astra editor core unavailable at %s: %s', path, exc)
    return None


class _Owned:
    def __init__(self, api):
        self.api = api
        self.handle = _Handle()

    def close(self):
        handle = self.handle
        self.handle = None
        if handle:
            self._free(handle)

    def __del__(self):
        self.close()

    def _live(self):
        if not self.handle:
            raise _NativeError(CLOSED)
        return self.handle


class _Prepared(_Owned):
    def _free(self, handle):
        self.api.free_prepared(handle)

    def inspect(self):
        result = _Result(C.sizeof(_Result), ABI)
        _check(self.api.get_result(self._live(), C.byref(result)))
        # Copy only the sparse routing evidence the Python sidecar needs. No
        # ctypes pointer or object referring to a borrowed array escapes here.
        changes = []
        for i in range(result.change_count):
            entry = result.changes[i]
            changes.append((entry.key, entry.flags, entry.after.order))
        normalized = tuple(result.normalized[i] for i in range(result.normalized_count))
        return bool(result.changed), tuple(changes), normalized


class _Session(_Owned):
    def __init__(self, api, seed):
        super().__init__(api)
        _check(api.open(C.byref(seed), C.byref(self.handle)))

    def _free(self, handle):
        self.api.free_session(handle)

    def stamp(self):
        stamp = _Stamp()
        _check(self.api.get_stamp(self._live(), C.byref(stamp)))
        return stamp

    def prepare(self, command):
        result = _Prepared(self.api)
        _check(self.api.prepare(self._live(), C.byref(command), C.byref(result.handle)))
        return result

    def bind(self, token, revision):
        _check(self.api.bind_revision(self._live(), C.byref(token), _revision_bytes(revision)))

    def accept(self, token):
        _check(self.api.accept(self._live(), C.byref(token)))

    def discard(self, token):
        _check(self.api.discard(self._live(), C.byref(token)))


class _Decline(Exception):
    """Not a lossless fast-path representation; use the existing Python editor."""


_ATOMS = (type(None), bool, int, float, str)


def _plain_json(value):
    t = type(value)
    if t is dict:
        return all(type(k) is str and _plain_json(v) for k, v in value.items())
    if t is list:
        return all(_plain_json(v) for v in value)
    return t in _ATOMS


def _same_json(a, b):
    # Cold certificate: include ordering and scalar representation. Equality
    # alone would miss int/float changes, key reorder, and a signed zero change.
    t = type(a)
    if t is not type(b):
        return False
    if t is dict:
        return len(a) == len(b) and all(type(k) is str and k == l and _same_json(v, w)
                                      for (k, v), (l, w) in zip(a.items(), b.items()))
    if t is list:
        return len(a) == len(b) and all(_same_json(v, w) for v, w in zip(a, b))
    if t is float and a == 0 and b == 0:
        return math.copysign(1, a) == math.copysign(1, b)
    return t in _ATOMS and (a is b or a == b)


def _copy_json(value):
    if type(value) is dict:
        return {k: _copy_json(v) for k, v in value.items()}
    if type(value) is list:
        return [_copy_json(v) for v in value]
    return value


def _nested(row):
    return any(type(v) in (list, dict) for v in row.values())


def _ident(value):
    if type(value) is not str:
        raise _Decline()
    return value


def _seconds(value):
    # Do not collapse distinct Python integer seconds to the same double.
    if type(value) is float or (type(value) is int and -(1 << 53) <= value <= (1 << 53)):
        return float(value)
    raise _Decline()


def _int32(value):
    if type(value) is not int or not -(1 << 31) <= value < (1 << 31):
        raise _Decline()
    return value


class _Group:
    def __init__(self, rows):
        if type(rows) is not list or any(type(row) is not dict for row in rows):
            raise _Decline()
        self.keys = list(range(1, len(rows) + 1))  # zero denotes a missing ID on input
        self.pos = {key: i for i, key in enumerate(self.keys)}
        self.by_id = {_ident(row['id']): key for key, row in zip(self.keys, rows)}
        self.complex = {key for key, row in zip(self.keys, rows) if _nested(row)}
        self.next_key = len(rows) + 1


class _Packed:
    def __init__(self, command, token, name, deletes, updates, adds):
        self.command, self.token, self.name = command, token, name
        self.deletes, self.updates, self.adds = deletes, updates, adds


class _Projection:
    def __init__(self, api, base, disk):
        from .legacy_fixed_points import retire_fixed_points
        from .midi import PPQ

        # A full check is a cold-load cost. Refuse to memoize migrations or
        # validation that would change the editable body (other than duration,
        # whose legacy normalization is applied only to the saved candidate).
        probe = retire_fixed_points(base)
        clock, end_beat = editing._validate_document(probe)
        duration = probe['duration']
        if 'duration' in base:
            probe['duration'] = base['duration']
        else:
            del probe['duration']
        if not _same_json(base, probe) or type(base['deleted_ids']) is not list:
            raise _Decline()
        self.api, self.body, self.disk = api, base, disk
        self.clock, self.ppq, self.duration = clock, PPQ, duration
        self.groups = {name: _Group(base[name]) for name in _NAMES}
        self.deleted = set(map(_ident, base['deleted_ids']))
        self.request = 0
        opening = None if base['tempo'].get('opening_policy',{}).get('score_only') else base['tempo'].get('score_start_seconds')
        closing = clock.data['score_end']['seconds'] - 1e-8 if end_beat is not None else 0.
        bounds = _Bounds((1 if opening is not None else 0) | (2 if end_beat is not None else 0),
                         opening - 1e-8 if opening is not None else 0., closing, duration + 1e-6)
        arrays = []
        for name in _NAMES:
            rows = base[name]
            array = (_Row * len(rows))()
            for i, row in enumerate(rows):
                array[i] = self.row(name, i + 1, row)
            arrays.append(array)
        notes, controls = arrays
        seed = _Seed(C.sizeof(_Seed), ABI, _revision_bytes(base['revision']), bounds,
                     notes, len(notes), controls, len(controls))
        self.session = _Session(api, seed)

    def tick(self, value):
        # TempoMap and Python round are the exact existing CC clock, including
        # extrapolation and ties to even. Native receives a bucket label, not
        # quantized seconds. Invalid intermediate assignments can be replaced.
        if not math.isfinite(value) or value < 0:
            return 0
        tick = round(self.clock.to_beat(value) * self.ppq)
        if not 0 <= tick <= _MAX_KEY:
            raise _Decline()
        return tick

    def row(self, name, key, row):
        channel = _int32(row.get('channel', 0))
        if name == 'notes':
            return _Row(key, 0, 0, _seconds(row['start']), _seconds(row['end']),
                        _int32(row['pitch']), _int32(row['velocity']), channel)
        time = _seconds(row['seconds'])
        number = _int32(row['number'])
        return _Row(key, 0, self.tick(time) if number == 64 else 0, time, 0., number, _int32(row['value']), channel)

    def pack(self, revision, op):
        name = op['type']
        group = self.groups[name]
        is_note = name == 'notes'
        deletes = list(map(_ident, op.get('delete', [])))
        deleted_keys = (_Key * len(deletes))(*(group.by_id.get(i, 0) for i in deletes))
        fields = ({'start': ('time', _TIME, _seconds), 'end': ('end', _END, _seconds),
                   'pitch': ('data1', _DATA1, _int32), 'velocity': ('data2', _DATA2, _int32)} if is_note else
                  {'seconds': ('time', _TIME, _seconds), 'value': ('data2', _DATA2, _int32)})
        updates = op.get('updates', [])
        native_updates = (_Update * len(updates))()
        final_fields = {}
        for i, update in enumerate(updates):
            if type(update) is not dict:
                raise _Decline()
            key = group.by_id.get(_ident(update.get('id')), 0)
            row, mask, raw = _Row(key=key), 0, {}
            for field, value in update.items():
                if field == 'id':
                    continue
                attr, flag, convert = fields[field]
                setattr(row, attr, convert(value))
                mask |= flag
                raw[field] = value
            if not is_note and mask & _TIME:
                row.tick = self.tick(row.time)
            native_updates[i] = _Update(row, mask)
            final_fields.setdefault(key, {}).update(raw)

        adds = op.get('add', [])
        if group.next_key + len(adds) - 1 > _MAX_KEY or self.request == _MAX_KEY:
            raise _Decline()
        native_adds = (_Row * len(adds))()
        added = {}
        seen = set()
        for i, event in enumerate(adds):
            if type(event) is not dict:
                raise _Decline()
            key = group.next_key + i
            row = deepcopy(event)
            row.update(id=editing.uuid4().hex, origin='manual')
            if row['id'] in group.by_id or row['id'] in seen:
                raise _Decline()
            seen.add(row['id'])
            row.setdefault('channel', 0)
            if not is_note:
                row.setdefault('number', 64)
            native_adds[i] = self.row(name, key, row)
            added[key] = row
        self.request += 1
        token = _Token(self.session.stamp(), self.request)
        command = _Command(C.sizeof(_Command), ABI, _revision_bytes(revision), token, 0 if is_note else 1,
                           deleted_keys, len(deleted_keys), native_updates, len(native_updates),
                           native_adds, len(native_adds))
        # The ctypes envelope owns references to its arrays for the call.
        return _Packed(command, token, name, deletes, final_fields, added)

    def view_copy(self, body, plan=None):
        # Full ordinary Python view, but no round-trip through C or JSON. Plain
        # rows take a C-level shallow dict copy. Only known nested rows need
        # recursive isolation. No mutable object from this cache reaches Qt.
        view = body.copy()
        for name, value in body.items():
            if name not in _NAMES:
                view[name] = _copy_json(value)
                continue
            group = self.groups[name]
            rows = list(map(dict.copy, value))
            edited = plan is not None and plan.name == name
            pos = plan.layout[1] if edited and plan.layout is not None else group.pos
            for key in group.complex:
                if not edited or key not in plan.removed:
                    at = pos[key]
                    rows[at] = _copy_json(value[at])
            if edited:
                for key in plan.add_complex:
                    at = plan.add_positions[key]
                    rows[at] = _copy_json(value[at])
            view[name] = rows
        return view

    def install(self, plan):
        # Under the same edit lock, after durable acceptance only. An exception
        # here discards the whole cache entry rather than exposing a partial map.
        group = self.groups[plan.name]
        old = self.body[plan.name]
        for key in plan.removed:
            del group.by_id[old[group.pos[key]]['id']]
        for _, key, row in plan.retained:
            group.by_id[row['id']] = key
        if plan.layout is not None:
            group.keys, group.pos = plan.layout
        else:
            group.keys.extend(key for _, key, _ in plan.retained)
            group.pos.update(plan.add_positions)
        group.complex.difference_update(plan.removed)
        group.complex.update(plan.add_complex)
        group.next_key += len(plan.packed.adds)
        self.deleted.update(plan.deleted)
        self.body = plan.body


class _Plan:
    def __init__(self, projection, packed, changes, normalized, index, operation):
        self.packed, self.name = packed, packed.name
        self.cacheable = False
        p = projection
        group = p.groups[self.name]
        old = p.body[self.name]
        self.removed, self.retained = set(), []
        replacements = {}
        for key, flags, order in changes:
            if flags & _BEFORE:
                if flags & _AFTER:
                    row = old[group.pos[key]].copy()
                    row.update(packed.updates[key])
                    if flags & _MANUAL:
                        row['origin'] = 'manual'
                    replacements[key] = row
                else:
                    self.removed.add(key)
            else:
                self.retained.append((order, key, packed.adds[key]))
        self.retained.sort()
        self.add_complex = {key for _, key, row in self.retained if _nested(row)}
        self.layout = None
        if self.removed:
            # The persisted array must be compact and keep input order. Only
            # removal requires a pass to compact/reindex that particular group.
            keys = [key for key in group.keys if key not in self.removed]
            rows = [replacements.get(key, old[group.pos[key]]) for key in keys]
            keys.extend(key for _, key, _ in self.retained)
            self.layout = keys, {key: i for i, key in enumerate(keys)}
        else:
            rows = old.copy()
            for key, row in replacements.items():
                rows[group.pos[key]] = row
        self.add_positions = {key: len(rows) + i for i, (_, key, _) in enumerate(self.retained)}
        rows.extend(row for _, _, row in self.retained)

        deleted = packed.deletes.copy()  # explicit deletions always append, even across groups
        appended = set(deleted)
        for key in normalized:
            row = packed.adds[key] if key in packed.adds else old[group.pos[key]]
            ident = row['id']
            if ident not in p.deleted and ident not in appended:
                deleted.append(ident)
                appended.add(ident)
        self.deleted = deleted
        body = p.body.copy()
        body[self.name] = rows
        if deleted:
            body['deleted_ids'] = body['deleted_ids'] + deleted
        body['range'] = _copy_json(index.get('range', body.get('range')))
        body['operation'] = deepcopy(operation)
        body['manual'] = True
        body['duration'] = p.duration
        self.body = body


# Bounded process-local projections, not a second writer or revision cache.
# Taking an entry returns a strong reference. Eviction never explicitly closes
# a session that another project's worker could still hold during CDLL or I/O.
_cache = OrderedDict()
_cache_lock = Lock()


def _key(path):
    return os.path.normcase(str(path.parent.resolve()))


def _forget(key, expected=None):
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and (expected is None or entry is expected):
            del _cache[key]


def invalidate(path):
    with _cache_lock:
        if not _cache:
            return
    try:
        _forget(_key(path))
    except (OSError, RuntimeError):
        # No token outlives an edit lock. A later checkout must still match disk.
        pass


def _remember(key, projection):
    evicted = None
    with _cache_lock:
        previous = _cache.get(key)
        _cache[key] = projection
        _cache.move_to_end(key)
        if len(_cache) > 2:
            evicted = _cache.popitem(last=False)
    del previous, evicted  # any final native release is outside the cache mutex


def _identity(path, revision):
    def marker(p):
        s = p.stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    return marker(path), marker(editing._revision_file(path.parent, revision))


def _checkout(api, path, index):
    key, revision = _key(path), index['revision']
    disk = _identity(path, revision)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry.api is api and entry.body['revision'] == revision and entry.disk == disk:
            _cache.move_to_end(key)
            return key, entry
    _forget(key)
    raw = editing._read_revision(path.parent, revision)
    persisted_initial = 'rhythm_initial' in raw.get('tempo', {})
    base = editing._revision_view(path.parent, index, raw)
    # revision_view may attach references into the caller's head dictionary.
    base['range'] = _copy_json(base['range'])
    base['exported_revision'] = _copy_json(base['exported_revision'])
    entry = _Projection(api, base, disk)
    if persisted_initial and 'rhythm_initial' in base['tempo']:
        _remember(key, entry)
    return key, entry


def _reference_v1_try_edit(project, path, index, operation):
    """Called only with edit_lock held and the disk revision CAS checked.

    None selects the original Python operation. Never select fallback after
    successful native prepare: from then on there is one attempt, one outcome.
    """
    if (os.environ.get('ASTRA_EDITOR_CORE') == '0' or type(operation) is not dict
            or operation.get('type') not in _NAMES):
        return None
    api = _load_api(os.environ.get('ASTRA_EDITOR_CORE_LIBRARY', ''))
    if api is None:
        return None
    key = projection = None
    revision = index['revision']
    try:
        if not all(type(operation.get(k, [])) is list for k in ('add', 'updates', 'delete')) or not _plain_json(operation):
            return None
        key, projection = _checkout(api, path, index)
        packed = projection.pack(revision, operation)
        prepared = projection.session.prepare(packed.command)
    except (_Decline, ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError):
        return None
    except _NativeError as exc:
        # Legacy emits the exact domain errors in its original validation
        # order, and can repair an invalid/more general base. No I/O or added-ID
        # observer has run for this speculative command.
        if exc.status not in (INVALID, MISSING, DUPLICATE, LIMIT):
            if key is not None:
                _forget(key, projection)
            _LOG.warning('Native editor preparation unavailable: %s', exc)
        return None

    committed = consumed = False
    plan = proposed = None
    token = packed.token
    try:
        changed, changes, normalized = prepared.inspect()
        if not changed:
            view = editing._revision_view(path.parent, index, projection.view_copy(projection.body))
            projection.session.accept(token)
            consumed = True
            return view

        plan = _Plan(projection, packed, changes, normalized, index, operation)
        added_observer = editing._added_event_observer.get()
        if added_observer is not None:
            for i, row in enumerate(packed.adds.values()):
                added_observer(packed.name, i, row['id'])
        proposed = editing.uuid4().hex
        projection.session.bind(token, proposed)
        index['undo'] = [*index.get('undo', []), revision]
        index['redo'] = []
        upstream = editing._commit_observer.get()

        def observe(saved_path, saved_index):
            nonlocal committed
            if saved_path == path and saved_index.get('revision') == proposed:
                committed = True
            if upstream is not None:
                upstream(saved_path, saved_index)

        def make_view(folder, new_index, body):
            view = editing._revision_view(folder, new_index, projection.view_copy(body, plan))
            # No warm reuse when a load could redo a filesystem-dependent
            # rhythm-initial migration or change the structural header.
            plan.cacheable = ('rhythm_initial' in body['tempo'] and
                {k: v for k, v in body.items() if k not in _STATIC_EXCEPT} ==
                {k: v for k, v in view.items() if k not in _STATIC_EXCEPT})
            return view

        scope = editing._commit_observer.set(observe)
        try:
            return editing._write_validated_revision(project, plan.body, index, proposed, make_view=make_view)
        finally:
            editing._commit_observer.reset(scope)
    finally:
        try:
            if committed:
                # _save also observes a successful replace followed by an
                # exception. Accept before releasing edit_lock in both cases.
                projection.session.accept(token)
                if plan.cacheable:
                    projection.install(plan)
                    projection.disk = _identity(path, proposed)
                    _remember(key, projection)
                else:
                    _forget(key, projection)
            elif not consumed:
                projection.session.discard(token)
        except Exception:
            _forget(key, projection)
            # Cache bookkeeping cannot undo or mask the authoritative outcome.
            _LOG.exception('Evicting Astra editor projection after transaction finalization')
        finally:
            prepared.close()
