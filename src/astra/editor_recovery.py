"""Project-local recovery storage. Call the disk policy under editing.edit_lock.

The checkpoint body is opaque here. Native validation is supplied by the
caller at the open/restore boundary, not by a second editor or JSON reducer.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
from time import time_ns
from uuid import uuid4

from . import editor_checkpoint as cp


_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}(?::[0-9a-f]{64})?')
_DISCARD = re.compile(r'discard-([0-9a-f]{32})-([0-9]{1,20})\.json')
_STAMP = '.astra-recovery-project.json'
_ROOT = '.astra-recovery'
SKIPPED = object()


class PublicationError(OSError):
    def __init__(self, outcome, cause):
        self.outcome = outcome
        label = {'not_published': '没有写入新版本',
                 'present_unconfirmed': '文件已更新，但未确认写入完成',
                 'indeterminate': '无法确认文件目前的状态'}[outcome]
        super().__init__(f'{label}：{str(cause) or type(cause).__name__}')


def _hex(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _plain(path, test):
    info = path.lstat()
    if not test(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise cp.CheckpointError('恢复存储中有链接或不受支持的文件，未替换。')
    return info


def _read(path):
    _plain(path, stat.S_ISREG)
    return cp._read_checkpoint_bytes(path)


def _optional(path):
    try:
        return _read(path)
    except FileNotFoundError:
        return None


def _sync_dir(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _directory(path, create=False):
    if create:
        path.mkdir(exist_ok=True)
    _plain(path, stat.S_ISDIR)
    if create:
        _sync_dir(path.parent)
    return path


def _exclusive(path, data):
    """Publish a new name only. Never replace an existing backup or archive."""
    tmp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.astra-stage-', delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt':
            from .editor_head_io import publish
            publish(tmp, path, mutable=False)
        else:
            os.link(tmp, path)  # same directory; fails if the destination exists
        tmp.unlink(missing_ok=True)
        _sync_dir(path.parent)
    finally:
        if tmp is not None:
            # This name was created by this invocation, not found by a sweep.
            # Do not mask the original publication error with cleanup errors.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _flush_existing(path, _data):
    _plain(path, stat.S_ISREG)
    with path.open('rb+') as stream:
        os.fsync(stream.fileno())
    _sync_dir(path.parent)


def verified_write(path, data, put, prior=None):
    try:
        put(path, data)
        if _read(path) != data:
            raise cp.CheckpointError('写入后校验失败。')
    except Exception as exc:
        try:
            seen = _optional(path)
        except (OSError, ValueError):
            outcome = 'indeterminate'
        else:
            if seen == data:
                outcome = 'present_unconfirmed'
            elif seen == prior and not getattr(exc, 'preserve_editor_temporary', False):
                outcome = 'not_published'
            else:
                outcome = 'indeterminate'
        raise PublicationError(outcome, exc) from exc


def _immutable(path, data, *, allow_same=False):
    old = _optional(path) if allow_same else None
    if old is not None and old != data:
        raise cp.CheckpointError('同名副本内容不同，未覆盖。')
    verified_write(path, data, _exclusive if old is None else _flush_existing, old)


@dataclass(frozen=True)
class Scope:
    editing: Path
    id: str
    origin: str


def scope(project, *, seed=None):
    root = Path(project).resolve(strict=True)
    editing = root / 'editing'
    try:
        directory = _plain(editing, stat.S_ISDIR)
    except FileNotFoundError:
        if seed is None:
            return None
        raise
    parent = _plain(root, stat.S_ISDIR)
    location = cp._digest(cp._encode([os.path.normcase(str(root)), parent.st_dev,
                                      parent.st_ino, directory.st_dev, directory.st_ino]))
    path = editing / _STAMP
    raw = _optional(path)
    if raw is None:
        if seed is None:
            return None
        if 'save_project' in seed.index or 'save_family' in seed.index:
            raise cp.CheckpointError('找不到此项目的恢复登记，未写入。')
        raw = cp._encode(dict(format='astra-recovery-project-v1', location=location,
                              id=uuid4().hex, origin=seed.identity))
        _immutable(path, raw)
    try:
        row = json.loads(raw)
        valid = (isinstance(row, dict) and set(row) == {'format', 'location', 'id', 'origin'}
                 and row['format'] == 'astra-recovery-project-v1'
                 and isinstance(row['location'], str) and re.fullmatch(r'[0-9a-f]{64}', row['location'])
                 and _hex(row['id']) and isinstance(row['origin'], str)
                 and _HASH.fullmatch(row['origin']) and cp._encode(row) == raw)
    except (TypeError, ValueError) as exc:
        raise cp.CheckpointError('项目恢复登记无法读取。') from exc
    if not valid:
        raise cp.CheckpointError('恢复登记不属于此项目位置或已损坏，未替换。')
    return Scope(editing, row['id'], row['origin'])


def registered(project):
    # Existence only: a bad registration also prevents silently seeding a head.
    for name in (_STAMP, 'checkpoint.previous.json'):
        try:
            (Path(project) / 'editing' / name).lstat()
            return True
        except FileNotFoundError:
            continue
    return False


def family(store, record):
    index = record.index
    if 'save_project' not in index and 'save_family' not in index:
        if record.identity == store.origin:
            return cp._digest(record.identity.encode('ascii'))[:32]
    elif (not record.legacy and index.get('save_project') == store.id
          and _hex(index.get('save_family'))):
        return index['save_family']
    raise cp.CheckpointError('正式文件或副本不属于此项目的恢复记录，未替换。')


def check_open(project, record):
    if 'save_project' in record.index or 'save_family' in record.index:
        store = scope(project)
        if store is None:
            raise cp.CheckpointError('找不到此项目的恢复登记。')
        family(store, record)


def bound_index(store, record, lineage):
    index = dict(record.index, save_project=store.id, save_family=lineage)
    if record.legacy:
        index.update(imported_legacy=True, applied_sequence=0)
    return index


def _area(store, name, create=False):
    return _directory(_directory(store.editing / _ROOT, create) / name, create)


def _name(kind, identifier):
    if kind not in ('auto', 'before') or not _hex(identifier):
        raise cp.CheckpointError('恢复副本编号无效。')
    return f'{kind}-{identifier}.bak'


def _backup(store, path, kind, identifier):
    if path.name != _name(kind, identifier):
        raise cp.CheckpointError('恢复副本名称不匹配。')
    rec = cp.unpack(_read(path))
    meta = rec.index.get('recovery')
    if (rec.index.get('save_project') != store.id or not isinstance(meta, dict)
            or set(meta) != {'format', 'kind', 'id', 'owner', 'created_ns', 'order'}
            or type(meta['format']) is not int or meta['format'] != 1
            or meta['kind'] != kind or meta['id'] != identifier or not _hex(meta['owner'])
            or type(meta['created_ns']) is not int or not 0 <= meta['created_ns'] < 2**63
            or type(meta['order']) is not int or not 0 <= meta['order'] < 2**127):
        raise cp.CheckpointError('恢复副本格式或项目不匹配。')
    family(store, rec)
    return rec


def _records(store, kind):
    try:
        folder = _area(store, 'active' if kind == 'auto' else 'protected')
    except FileNotFoundError:
        return
    for path in folder.iterdir():
        match = re.fullmatch(kind + r'-([0-9a-f]{32})\.bak', path.name)
        if match:
            try:
                rec = _backup(store, path, kind, match[1])
            except (OSError, TypeError, ValueError):
                continue  # an unreadable, foreign or damaged name is not ours to rotate
            yield path, rec


def rotate(store, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('恢复副本数量应在 1 到 100 之间。')
    rows = sorted((rec.index['recovery']['order'], path.name, path, rec.identity)
                  for path, rec in _records(store, 'auto'))
    for _, _, path, digest in rows[:-limit]:
        rec = _backup(store, path, 'auto', path.stem[5:])
        if rec.identity != digest:
            raise cp.CheckpointError('旧副本已变化，未移走该文件。')
        from .editor_recycle import recycle_verified
        recycle_verified(path, path.parent)


def write_backup(store, index, body, job, permit, *, history=None, instrument=None):
    folder = _area(store, 'active', True)
    path = folder / _name('auto', job.id)
    now = _optional(path)
    if now is None:
        order = max(time_ns(), 1 + max((rec.index['recovery']['order']
                                       for _, rec in _records(store, 'auto')), default=0))
    else:
        order = _backup(store, path, 'auto', job.id).index['recovery']['order']
    meta = dict(format=1, kind='auto', id=job.id, owner=index['checkpoint_owner'],
                created_ns=job.created_ns, order=order)
    data = cp.pack(dict(index, recovery=meta), body, history=history, instrument=instrument)
    # This is the publication boundary for disabling autosave. No GUI lock is
    # held across fsync, read-back, or rotation.
    limit = permit()
    if limit is None:
        return SKIPPED
    _immutable(path, data, allow_same=True)
    try:
        rotate(store, limit)
    except (OSError, TypeError, ValueError) as exc:
        return f'副本已保存，但旧副本尚未全部归档：{exc}'
    return None


def _discard_data(store, owner, sequence):
    return cp._encode(dict(format='astra-discard-v1', project=store.id,
                           owner=owner, through=sequence))


def discard_through(store, owner, sequence):
    folder = _area(store, 'discarded', True)
    _immutable(folder / f'discard-{owner}-{sequence}.json',
               _discard_data(store, owner, sequence), allow_same=True)


def _discarded(store):
    cutoffs = {}
    try:
        folder = _area(store, 'discarded')
    except FileNotFoundError:
        return cutoffs
    for path in folder.iterdir():
        match = _DISCARD.fullmatch(path.name)
        if match:
            owner, end = match[1], int(match[2])
            if end >= 2**64 or _read(path) != _discard_data(store, owner, end):
                # Fail closed for normal offers if the discard receipt cannot be trusted.
                raise cp.CheckpointError('无法核对已放弃的副本，未提供自动恢复提示。')
            cutoffs[owner] = max(end, cutoffs.get(owner, -1))
    return cutoffs


def _formal(project, store):
    raw = _optional(store.editing / 'current.json')
    if raw is None:
        return None, None, 'missing'
    try:
        record = cp.decode(project, raw)
    except cp.CheckpointError:
        return raw, None, 'damaged:' + cp._digest(raw)
    family(store, record)  # a readable, unrelated head is not a damaged head
    return raw, record, record.identity


def _usable(record, validate):
    if record is None:
        return False
    try:
        validate(record)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
    return True


def _eligible(store, rec, formal, usable, cutoffs):
    meta = rec.index['recovery']
    return (rec.index['applied_sequence'] > cutoffs.get(meta['owner'], -1)
            and (not usable or (family(store, rec) == family(store, formal)
                 and rec.index['applied_sequence'] > formal.index.get('applied_sequence', 0))))


def _option(rec, kind, identifier, stamp):
    return dict(id=identifier, kind=kind, digest=rec.identity, created_ms=stamp // 10**6,
                sequence=str(rec.index['applied_sequence']), archive_identity=rec.index.get('archive_identity'))


def _previous(store):
    rec = cp.unpack(_read(store.editing / 'checkpoint.previous.json'))
    if rec.index.get('save_project') != store.id:
        raise cp.CheckpointError('旧版前次保存没有可核对的项目身份。')
    family(store, rec)
    return rec


def options_locked(project, validate, *, include_protected=False):
    store = scope(project)
    if store is None:
        return None
    _, formal, identity = _formal(project, store)
    usable = _usable(formal, validate)
    cutoffs = _discarded(store)
    rows = []
    for kind in ('auto', 'before') if include_protected else ('auto',):
        for _, rec in _records(store, kind):
            meta = rec.index['recovery']
            if (kind == 'before' or _eligible(store, rec, formal, usable, cutoffs)) and _usable(rec, validate):
                rows.append((meta['order'], _option(rec, kind, meta['id'], meta['created_ns'])))
    rows.sort(key=lambda row: (row[0], row[1]['id']), reverse=True)
    choices = [row[1] for row in rows]
    if not usable:
        try:
            rec = _previous(store)
            if _usable(rec, validate):
                stamp = (store.editing / 'checkpoint.previous.json').stat().st_mtime_ns
                choices.append(_option(rec, 'previous', 'previous', stamp))
        except (OSError, ValueError):
            pass
    return dict(project=store.id, head=identity, formal_available=usable, options=choices)


def protect_legacy_archive(store, record, lineage):
    """Bind the exact locally archived pre-upgrade image to this recovery store."""
    token = uuid4().hex
    now = time_ns()
    index = bound_index(store, record, lineage)
    index['archive_identity'] = cp._digest(record.raw)
    index['recovery'] = dict(format=1, kind='before', id=token, owner=token,
                             created_ns=now, order=now)
    _immutable(_area(store, 'protected', True) / _name('before', token),
               cp.pack(index, record.body, history=record.history, instrument=record.instrument))


def restore_locked(project, offer, identifier, validate):
    store = scope(project)
    if not store or store.id != offer['project']:
        raise cp.CheckpointError('项目已变化，请重新查看恢复副本。')
    raw, formal, identity = _formal(project, store)
    if identity != offer['head']:
        raise cp.CheckpointError('正式版本已变化，请重新打开项目后再选择。')
    row = next((row for row in offer['options'] if row['id'] == identifier), None)
    if row is None:
        raise cp.CheckpointError('请选择列表中的恢复副本。')
    kind = row['kind']
    usable = _usable(formal, validate)
    if kind == 'previous' and identifier == 'previous' and not usable:
        selected = _previous(store)
    elif kind in ('auto', 'before'):
        folder = _area(store, 'active' if kind == 'auto' else 'protected')
        selected = _backup(store, folder / _name(kind, identifier), kind, identifier)
        if kind == 'auto' and not _eligible(store, selected, formal, usable, _discarded(store)):
            raise cp.CheckpointError('所选副本已不适用于此次恢复。')
    else:
        raise cp.CheckpointError('所选副本已不适用于此次恢复。')
    if selected.identity != row['digest']:
        raise cp.CheckpointError('副本已变化，请重新选择。')
    validate(selected)
    if raw is not None:
        try:
            archived = _area(store, 'protected', True)
            token = uuid4().hex
            _immutable(archived / f'before-{token}.current.json', raw)
            if formal is not None:
                index = bound_index(store, formal, family(store, formal))
                now = time_ns()
                index['recovery'] = dict(format=1, kind='before', id=token,
                                         owner=token, created_ns=now, order=now)
                _immutable(archived / _name('before', token), cp.pack(index, formal.body, history=formal.history, instrument=formal.instrument))
        except (OSError, TypeError, ValueError) as exc:
            raise cp.CheckpointError('未能确认原文件的保护副本，正式文件未替换。') from exc
    if _formal(project, store)[2] != identity:
        raise cp.CheckpointError('保护副本写入期间正式文件已变化，未替换。')
    # Starting a new recovery family prevents older higher sequence numbers
    # from being mistaken for new work after restoring a lower sequence.
    index = bound_index(store, selected, uuid4().hex)
    index.pop('recovery', None)
    index.update(checkpoint_id=uuid4().hex, checkpoint_owner=uuid4().hex)
    verified_write(store.editing / 'current.json', cp.pack(index, selected.body, history=selected.history, instrument=selected.instrument),
                   cp.atomic, raw)
    return index['revision']


def dismiss_locked(project, offer):
    """Mark all offered auto-recovery backups as explicitly ignored by the user."""
    store = scope(project)
    if not store or store.id != offer['project']:
        raise cp.CheckpointError('项目已变化，请重新查看恢复副本。')
    if not offer.get('formal_available'):
        raise cp.CheckpointError('当前正式文件不可用，不能忽略恢复提示。')
    _, formal, identity = _formal(project, store)
    if identity != offer['head'] or formal is None:
        raise cp.CheckpointError('正式版本已变化，请重新打开项目后再操作。')
    cutoffs = {}
    folder = _area(store, 'active')
    for row in offer.get('options') or ():
        if row.get('kind') != 'auto':
            continue
        try:
            rec = _backup(store, folder / _name('auto', row['id']), 'auto', row['id'])
        except (OSError, TypeError, ValueError):
            continue
        owner = rec.index['recovery']['owner']
        seq = rec.index['applied_sequence']
        cutoffs[owner] = max(seq, cutoffs.get(owner, -1))
    for owner, seq in cutoffs.items():
        discard_through(store, owner, seq)
    return len(cutoffs)
