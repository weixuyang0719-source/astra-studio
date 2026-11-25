"""Atomically published Notes + generated score, via the sealed formal checkpoint.

project.json remains the base metadata for ordinary jobs. For this command the
single authoritative commit is editing/current.json; no required mirror write.
"""
from collections import OrderedDict
from pathlib import Path
from threading import Lock
import os

_cache = OrderedDict()
_cache_lock = Lock()
_HEAD_LINE_MAX = 64 * 1024


KEY = 'notes_score_publication'
NATIVE_KEY = 'native_notes_score'
NATIVE_FILE = 'piano.mscz'
_FIELDS = ('latest_export', 'latest_score_export', 'latest_pdf_export', 'latest_harmony_export',
           'editor_exported_revision', 'editor_score_revision', NATIVE_KEY)


def marks(state):
    return {key: state.get(key) for key in _FIELDS}


def effective(project, raw, record):
    if record is None or record.legacy:
        return raw
    return _apply(project, raw, record.index.get(KEY))


def _apply(project, raw, saved):
    if not isinstance(raw, dict) or not isinstance(saved, dict) or marks(raw) != saved.get('base'):
        # An ordinary export wrote a new project.json. It carries the effective
        # state forward when appropriate and is no longer shadowed by this head.
        return raw
    from .provisional_score import draft_directory
    token, source, rev = saved.get('token'), saved.get('source'), saved.get('revision')
    if not all(isinstance(k, str) and k for k in (token, source, rev)):
        return raw
    try:
        export = str(draft_directory(project, token))
    except (OSError, ValueError):
        return raw
    out = dict(raw, latest_export=export, latest_score_export=export,
               editor_exported_revision=rev, editor_score_revision=rev)
    out[NATIVE_KEY] = dict(token=token, source=source)
    if saved.get('pdf') is True:
        out['latest_pdf_export'] = export
    if saved.get('harmony') is True:
        out['latest_harmony_export'] = export
    return out


def _open_head(path):
    if os.name == 'nt':
        from .editor_head_io import open_reader
        return open_reader(path)
    return path.open('rb')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_project(path, raw=None):
    from . import editor_checkpoint as cp
    path = Path(path)
    if raw is None:
        from .pipeline import read_raw_json
        raw = read_raw_json(path)
    if not isinstance(raw, dict):
        return raw
    head = path.parent/'editing'/'current.json'
    cache_key = os.path.normcase(str(head.absolute()))
    with _cache_lock:
        known = _cache.get(cache_key)
    try:
        from .project_access import _plain
        _plain(head.parent, directory=True)
        stamp = _stamp(_plain(head, directory=False))
        if known is not None and known[0] == stamp:
            return _apply(path.parent, raw, known[1])
        with _open_head(head) as reader:
            stamp = _stamp(os.fstat(reader.fileno()))
            line = reader.readline(_HEAD_LINE_MAX+1)
            if not line.startswith(cp._CHECKPOINT_PREFIXES) or b'"notes_score_publication":' not in line:
                saved = None
            else:
                # Old/ordinary projects never parse/allocate the Notes body or
                # undo history here. Verify the bundle once per committed file.
                saved = cp.unpack(line+reader.read()).index.get(KEY)
    except FileNotFoundError:
        return _apply(path.parent, raw, known[1]) if known else raw
    except OSError:
        if known and known[1]:
            return _apply(path.parent, raw, known[1])
        raise
    except ValueError:
        # Keep the app's existing damaged-checkpoint recovery workflow.
        return raw
    with _cache_lock:
        _cache[cache_key] = (stamp, saved)
        _cache.move_to_end(cache_key)
        if len(_cache) > 32:
            _cache.popitem(last=False)
    return _apply(path.parent, raw, saved)


def native_path(project, state, source):
    from .provisional_score import draft_directory
    ref = state.get(NATIVE_KEY)
    if not isinstance(ref, dict) or not source or ref.get('source') != source:
        return None
    path = draft_directory(project, ref.get('token')) / NATIVE_FILE
    parent = path.parent
    if (parent.is_symlink() or parent.resolve(strict=True) != parent or path.is_symlink()
            or not path.is_file() or path.stat().st_nlink != 1
            or not 0 < path.stat().st_size <= 64 * 1024 * 1024):
        raise ValueError('无法核实已保存的更新谱面。')
    from .project_access import approved
    grant = approved(project)
    return grant.file(path) if grant is not None else path


def binding(receipt, proof, old):
    # Caller holds project_lock then edit_lock. The future native score is not
    # reachable as a project's current score until this and Notes are sealed.
    from .provisional_score import _object, _regular, draft_directory, pointer_marks
    raw = _object(receipt.project, 'project.json')
    state = effective(receipt.project, raw, old)
    if pointer_marks(state) != dict(receipt.before):
        raise ValueError('工程已由其他操作改变，本次更新未保存。')
    if (draft_directory(receipt.project, receipt.token) != receipt.export
            or receipt.export.resolve(strict=True) != receipt.export
            or not proof or NATIVE_FILE not in dict(proof)
            or tuple((k, _regular(receipt.export, k)[1]) for k, _ in proof) != proof):
        raise ValueError('生成谱面在保存前已改变。')
    files = dict(receipt.artifacts)
    return dict(base=marks(raw), token=receipt.token, source=receipt.source,
                revision=receipt.revision, pdf='piano.pdf' in files, harmony='piano_harmony.json' in files)
