"""Bounded, inventory-only VST3 discovery. No native loads or writes."""
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock

from astra.vst_host.catalog import architecture, standard_roots

_MAX_MODULES = 2048
_MAX_ENTRIES = 32768
_MAX_DEPTH = 16
_MAX_BYTES = 4 * 1024 * 1024
_MAX_CLASSES = 1024
_EMPTY = ((), {})
_snapshot = _EMPTY
_publish_lock = Lock()
_scan_lock = Lock()


@dataclass(frozen=True)
class MetadataClass:
    module: str
    cid: str
    name: str
    vendor: str | None
    category: str
    kind: str


def _plain(path):
    mode = path.lstat()
    if stat.S_ISLNK(mode.st_mode) or getattr(mode, 'st_file_attributes', 0) & 0x400:
        raise ValueError('VST3 path is a reparse point')
    if not (stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode)):
        raise ValueError('VST3 path is not a regular file or directory')
    return mode


def _modules(roots):
    found = {}
    visits = 0
    limited = False
    for root in roots:
        root = Path(root).absolute()
        try:
            # Do not follow reparse points, including ancestors. Automatic
            # discovery never expands configured paths or project-supplied paths.
            if str(root).startswith(chr(92)*2):
                continue
            for parent in (*reversed(root.parents), root):
                _plain(parent)
        except (OSError, ValueError):
            continue
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        visits += 1
                        if visits > _MAX_ENTRIES or len(found) >= _MAX_MODULES:
                            return found.values(), True
                        path = Path(entry.path)
                        try:
                            mode = _plain(path).st_mode
                        except (OSError, ValueError):
                            continue
                        if path.suffix.lower() == '.vst3':
                            found[str(path).casefold()] = path
                        elif stat.S_ISDIR(mode):
                            if depth < _MAX_DEPTH:
                                stack.append((path, depth+1))
                            else:
                                limited = True
            except OSError:
                continue
    return found.values(), limited


def _validated_architecture(module):
    if stat.S_ISDIR(_plain(module).st_mode):
        folder = module / 'Contents' / 'x86_64-win'
        _plain(folder.parent)
        _plain(folder)
        # The existing host requires the expected filename or exactly one
        # *.vst3 fallback. Reject reparse candidates in either case.
        expected = folder / module.name
        try:
            _plain(expected)
        except FileNotFoundError:
            with os.scandir(folder) as entries:
                candidates = []
                for i, entry in enumerate(entries):
                    if i >= _MAX_CLASSES:
                        raise ValueError('VST3 binary directory exceeds limit')
                    if entry.name.lower().endswith('.vst3'):
                        candidates.append(Path(entry.path))
                        if len(candidates) > 1:
                            raise ValueError('Ambiguous VST3 binaries')
            if len(candidates) != 1:
                raise ValueError('No VST3 binary')
            _plain(candidates[0])
    return architecture(module)


def _label(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        return None
    value = value.strip()
    return value if value and all(c.isprintable() for c in value) else None


def manifest_classes(module):
    manifest = module / 'Contents' / 'Resources' / 'moduleinfo.json'
    for path in (manifest.parent.parent, manifest.parent, manifest):
        _plain(path)
    with manifest.open('rb') as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError('VST3 moduleinfo exceeds limit')
    raw = re.sub(r'"(?:[^"\\]|\\.)*"|,\s*(?=[}\]])',
                 lambda match: match[0] if match[0].startswith('"') else '',
                 raw.decode('utf-8-sig'))
    data = json.loads(raw)
    if (not isinstance(data, dict) or not isinstance(data.get('Classes'), list)
            or len(data['Classes']) > _MAX_CLASSES):
        raise ValueError('Invalid VST3 moduleinfo')
    factory = data.get('Factory Info')
    vendor = _label(factory.get('Vendor')) if isinstance(factory, dict) else None
    result = []
    for row in data['Classes']:
        if not isinstance(row, dict) or row.get('Category') != 'Audio Module Class':
            continue
        cid, name, categories = row.get('CID'), _label(row.get('Name')), row.get('Sub Categories')
        if not isinstance(cid, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', cid):
            continue
        if not name or not isinstance(categories, list) or len(categories) > 32:
            continue
        tags = [_label(value) for value in categories]
        if not all(tags):
            continue
        roles = {value.casefold() for value in tags} & {'instrument', 'fx'}
        for role in sorted(roles):
            result.append(MetadataClass(str(module), cid.lower(), name,
                                        _label(row.get('Vendor')) or vendor,
                                        ' | '.join(tags), role))
    return tuple(result)


def inspect_metadata(roots=None):
    rows = []
    incompatible = 0
    modules, limited = _modules(standard_roots() if roots is None else roots)
    eligible = set()
    for module in modules:
        try:
            if _validated_architecture(module) != 'x64':
                incompatible += 1
                continue
        except (OSError, ValueError):
            incompatible += 1
            continue
        eligible.add(str(module))
        try:
            rows.extend(manifest_classes(module))
        except (OSError, ValueError, RecursionError):
            pass
    # A persisted auto key has only a CID, not a path. Never let an
    # ambiguous CID silently switch a project to a different module.
    accepted, ambiguous = unambiguous_classes(rows)
    return accepted, {'modules': len(modules),
                      'unresolved': len(eligible - {r.module for r in accepted}),
                      'incompatible': incompatible,
                      'ambiguous': ambiguous,
                      'limited': limited}


def unambiguous_classes(rows):
    # One class can legitimately be present under two roles. A CID must
    # still never resolve to different modules or duplicate role rows.
    modules = {}
    for row in rows:
        modules.setdefault(row.cid, set()).add(row.module)
    counts = Counter((row.cid, row.kind) for row in rows)
    ambiguous = {cid for cid, paths in modules.items() if len(paths) > 1}
    ambiguous.update(cid for (cid, _), count in counts.items() if count > 1)
    return (tuple(sorted((row for row in rows if row.cid not in ambiguous),
                         key=lambda row: (row.name.casefold(), row.cid, row.kind))),
            len(ambiguous))


def scan_catalog():
    """Call ONLY from the setup worker; publish a whole nonblocking snapshot."""
    global _snapshot
    snap = _snapshot
    if snap is not _EMPTY:
        return snap
    with _scan_lock:
        if _snapshot is _EMPTY:
            from .vst_refresh import inventory
            initial = inventory()
            with _publish_lock:
                if _snapshot is _EMPTY:
                    _snapshot = initial
        return _snapshot


def static_catalog():
    """Never initiate I/O, including on an early mount or settings action."""
    return _snapshot


def additional_classes(existing, metadata, kind):
    known = {cid.lower() for cid in existing.values()}
    for row in metadata:
        key = f'vst3-{row.cid}'
        if row.kind == kind and row.cid not in known and key not in existing:
            yield key, row


def refresh_catalog(library,progress=None,retry_failed=True):
    global _snapshot
    from .vst_refresh import inventory
    refreshed = inventory(library=library, refresh=True, progress=progress, retry_failed=retry_failed)
    with _publish_lock:
        _snapshot = refreshed
    return refreshed
