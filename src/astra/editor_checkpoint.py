"""Verified working checkpoints. This module has no editor command executor."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

_PREFIX = b'{"_astra_checkpoint":1,'
_PREFIX2 = b'{"_astra_checkpoint":2,'
_PREFIX3 = b'{"_astra_checkpoint":3,'
_CHECKPOINT_PREFIXES = (_PREFIX, _PREFIX2, _PREFIX3)
_HISTORY_PREFIXES = (_PREFIX2, _PREFIX3)
_SPLIT = b',"document":\n'
_HISTORY_SPLIT = b',"history":\n'
_INSTRUMENT_SPLIT = b',"instrument_attachment":\n'
_MAX_INSTRUMENT_BYTES = 64 * 1024 * 1024
_SEAL = b',"_checkpoint_sha256":"'


class CheckpointError(ValueError):
    pass


def _encode(obj):
    return json.dumps(obj, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def _digest(data):
    return sha256(data).hexdigest()


def _seal(header, body, history=None, instrument=None):
    h = sha256(header)
    h.update(b'\0')
    h.update(body)
    if history is not None:
        h.update(b'\0')
        h.update(history)
    if instrument is not None:
        h.update(b'\0')
        h.update(instrument)
    return h.hexdigest().encode('ascii')


def instrument_reference(ref):
    """Checkpoint/native wire reference only. No filenames or vendor data."""
    if ref is None:
        return
    label = ref.get('instrument') if isinstance(ref, dict) else None
    valid_label = isinstance(label, str) and 0 < len(label) <= 256
    try:
        if valid_label:
            label.encode('utf-8')  # Reject lone surrogates, as native UTF-8 parsing does.
    except UnicodeError as exc:
        raise CheckpointError('工程音色引用无效。') from exc
    if (not isinstance(ref, dict) or set(ref) != {'version', 'instrument', 'class_id', 'sha256', 'size'}
            or type(ref['version']) is not int or ref['version'] != 1
            or not valid_label
            or not isinstance(ref['class_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', ref['class_id'])
            or not isinstance(ref['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', ref['sha256'])
            or type(ref['size']) is not int or not 0 < ref['size'] <= _MAX_INSTRUMENT_BYTES):
        raise CheckpointError('工程音色引用无效。')


def _check_instrument(index, instrument):
    ref = index.get('instrument_state')
    instrument_reference(ref)
    if ref is None:
        if instrument is not None:
            raise CheckpointError('工程音色附件没有有效引用。')
    elif (type(instrument) is not bytes or len(instrument) != ref['size'] or _digest(instrument) != ref['sha256']):
        raise CheckpointError('工程音色附件缺失或校验失败。')


def pack(index, body, *, history=None, instrument=None):
    _check_instrument(index, instrument)
    if instrument is not None:
        if not history:
            raise CheckpointError('工程音色只能封存于完整工作快照。')
        # The label and opaque bytes sit outside the native document/history
        # JSON. Explicit boundaries and the envelope seal cover every byte.
        header = _PREFIX3 + _encode(dict(body_size=len(body), history_size=len(history), index=index))[1:]
        suffix = (_HISTORY_SPLIT, history, _INSTRUMENT_SPLIT, instrument)
    elif history is None:
        header = _PREFIX + _encode(index)[1:]
        suffix = ()
    else:
        # No top-level revision in v2. A legacy reader cannot mistake the
        # bundle for a usable revision index, even when the old body exists.
        header = _PREFIX2 + _encode(dict(body_size=len(body), index=index))[1:]
        suffix = (_HISTORY_SPLIT, history)
    return b''.join((header[:-1], _SEAL, _seal(header, body, history, instrument), b'"', _SPLIT, body, *suffix, b'\n}\n'))


@dataclass(frozen=True)
class Record:
    index: dict
    body: bytes
    identity: str
    raw: bytes
    legacy: bool = False
    history: bytes = None
    instrument: bytes = None

    def backup(self):
        if not self.legacy:
            return self.raw
        return pack(dict(self.index, imported_legacy=True,
                         applied_sequence=self.index.get('applied_sequence', 0)), self.body)


def legacy_stacks(index):
    from .editing import _revision_file
    undo, redo = index.get('undo', []), index.get('redo', [])
    if not isinstance(undo, list) or not isinstance(redo, list):
        raise CheckpointError('Legacy undo/redo must be revision arrays; nothing was overwritten.')
    seen = set()
    try:
        for stack in ((index['revision'],), undo, redo):
            for rev in stack:
                _revision_file(Path('.'), rev)
                if rev in seen:
                    raise ValueError('duplicate revision in history')
                seen.add(rev)
    except (KeyError, ValueError, TypeError) as exc:
        raise CheckpointError('Invalid legacy revision reference; nothing was overwritten.') from exc
    return undo, redo


def unpack(raw):
    split = raw.find(_SPLIT)
    prefix, seal_sep, seal = raw[:max(split, 0)].rpartition(_SEAL)
    if not (raw.startswith(_CHECKPOINT_PREFIXES) and split >= 0 and seal_sep and raw.endswith(b'\n}\n')):
        raise CheckpointError('工作快照格式损坏或不受支持；没有自动替换或重放编辑。')
    header = prefix + b'}'
    start, stop = split + len(_SPLIT), len(raw) - 3
    body_end = history_end = stop
    version = next(i for i, p in enumerate(_CHECKPOINT_PREFIXES, 1) if raw.startswith(p))
    history = instrument = None
    try:
        envelope = json.loads(header)
        if not isinstance(envelope, dict) or envelope.pop('_astra_checkpoint', None) != version:
            raise ValueError('bad version')
        index = envelope
        if version >= 2:
            size, index = envelope['body_size'], envelope['index']
            if (type(size) is not int or not 0 < size < stop - start
                    or not raw.startswith(_HISTORY_SPLIT, start + size, stop)):
                raise ValueError('bad body boundary')
            body_end = start + size
            history_start = body_end + len(_HISTORY_SPLIT)
            if version == 3:
                span = envelope['history_size']
                if (type(span) is not int or not 0 < span < stop - history_start
                        or not raw.startswith(_INSTRUMENT_SPLIT, history_start + span, stop)):
                    raise ValueError('bad instrument attachment boundary')
                history_end = history_start + span
                instrument = raw[history_end + len(_INSTRUMENT_SPLIT):stop]
            history = raw[history_start:history_end]
            if (not history or not isinstance(index, dict) or 'undo' in index or 'redo' in index
                    or type(index.get('can_undo')) is not bool or type(index.get('can_redo')) is not bool):
                raise ValueError('bad history manifest')
        if (not isinstance(index.get('revision'), str) or type(index.get('applied_sequence')) is not int
                or not 0 <= index['applied_sequence'] < 2**64):
            raise ValueError('bad index')
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointError('工作快照清单无效。') from exc
    body = raw[start:body_end]
    if seal != _seal(header, body, history, instrument) + b'"':
        raise CheckpointError('工作快照校验失败；可检查上一个已验证的保存。')
    if version == 1:
        legacy_stacks(index)
    _check_instrument(index, instrument)
    return Record(index, body, _digest(raw), raw, history=history, instrument=instrument)


def _read_checkpoint_bytes(path):
    path=Path(path)
    if os.name=='nt' and path.parent.name=='editing' and path.name in ('current.json','checkpoint.previous.json'):
        from .editor_head_io import open_reader
        with open_reader(path) as stream:
            return stream.read()
    return path.read_bytes()


def _read_record(folder, raw):
    from .editing import _revision_file
    if raw.startswith(_CHECKPOINT_PREFIXES):
        return unpack(raw)
    try:
        index = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(index, dict) or {'_astra_checkpoint', '_checkpoint_sha256', 'document'} & index.keys():
            raise ValueError('not a legacy index or supported bundle')
        legacy_stacks(index)
        body = _revision_file(folder, index['revision']).read_bytes()
    except CheckpointError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise CheckpointError('无法读取旧工程索引或当前正文；未自动覆盖。') from exc
    return Record(index, body, _digest(raw) + ':' + _digest(body), raw, True)


def read(project):
    folder = Path(project) / 'editing'
    try:
        raw = _read_checkpoint_bytes(folder / 'current.json')
    except FileNotFoundError:
        return None
    return _read_record(folder, raw)


def export_fields(project, index):
    from .pipeline import read_json
    path = Path(project) / 'project.json'
    try:
        state = read_json(path)
    except FileNotFoundError:
        state = {}
    return dict(exported_revision=state.get('editor_exported_revision', index.get('exported_revision')),
                score_revision=state.get('editor_score_revision', index.get('score_revision')))


def atomic(path, data):
    from .pipeline import _replace_editor_json
    path = Path(path)
    tmp = None
    preserve_temporary=False
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.checkpoint-', delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name=='nt' and path.parent.name=='editing' and path.name in ('current.json','checkpoint.previous.json'):
            from .editor_head_io import publish
            publish(tmp,path,mutable=True)
        else:
            _replace_editor_json(tmp,path)
    except Exception as error:
        preserve_temporary=bool(getattr(error,'preserve_editor_temporary',False))
        raise
    finally:
        if tmp is not None and not preserve_temporary:
            tmp.unlink(missing_ok=True)


def previous(project):
    folder = Path(project) / 'editing'
    return _read_record(folder, _read_checkpoint_bytes(folder / 'checkpoint.previous.json'))


def archive_raw(folder, raw):
    """Keep exact pre-upgrade/recovery bytes. Caller owns the edit lock."""
    identity = _digest(raw)
    path = folder / ('checkpoint.archive-' + identity + '.json')
    if path.is_symlink():
        raise CheckpointError('Checkpoint archive path is a symlink; nothing was overwritten.')
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    if existing is not None and existing != raw:
        raise CheckpointError('已有恢复归档校验失败。')
    # Also cross the barrier on a retry. A matching directory entry can have
    # been left by a replace whose durability barrier did not finish.
    if existing is None:
        atomic(path, raw)
    elif os.name == 'nt':
        from .editor_head_io import _flush_head
        _flush_head(path)
    else:
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return identity


def archived(project, identity):
    if (not isinstance(identity, str) or len(identity) != 64
            or any(c not in '0123456789abcdef' for c in identity)):
        raise CheckpointError('Invalid checkpoint archive identifier')
    folder = Path(project) / 'editing'
    raw = (folder / ('checkpoint.archive-' + identity + '.json')).read_bytes()
    if _digest(raw) != identity:
        raise CheckpointError('Checkpoint archive checksum mismatch')
    return _read_record(folder, raw)


def decode(project, raw):
    return _read_record(Path(project) / 'editing', raw)


def permits_export(project, guard, sequence):
    record = read(project)
    return bool(record and (record.identity == guard['anchor'] or
        (record.index.get('checkpoint_owner') == guard['lineage'] and
         record.index['applied_sequence'] <= sequence)))


from .editor_save_queue import Save, Saves, Writer
