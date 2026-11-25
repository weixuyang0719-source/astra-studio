"""Read-only preparation for recent-project navigation. No Qt objects cross threads."""
from pathlib import Path
import hashlib
import json

def source_stamp(project):
    # One call-local read per file: parse and hash the identical bytes.
    # Keep failures local too; never cache across version checks or requests.
    raw_cache={}
    def _read_once(path):
        if path not in raw_cache:
            try:raw_cache[path]=path.read_bytes()
            except OSError as exc:raw_cache[path]=exc
        value=raw_cache[path]
        if isinstance(value,OSError):raise value
        return value
    project=Path(project)
    paths=[project/'project.json',project/'editing/current.json']
    index={}
    try:index=json.loads(_read_once(paths[1]).decode('utf-8'))
    except (OSError,ValueError):pass
    if not isinstance(index,dict):index={}
    for revision in [index.get('revision'),*(index.get('undo') or [])[:1]]:
        if revision:paths.append(project/'editing/revisions'/f'{revision}.json')
    if index.get('revision'):
        revision_path=project/'editing/revisions'/f"{index['revision']}.json"
        try:revision=json.loads(_read_once(revision_path).decode('utf-8'))
        except (OSError,ValueError):revision={}
        if not isinstance(revision,dict):revision={}
        export=revision.get('source_export')
        if export:paths.append(Path(export)/'tempo_map.json')
    # Revision documents are immutable; include their contents to reject external edits.
    result=[]
    for path in paths:
        try:data=_read_once(path);result.append((str(path),hashlib.sha256(data).hexdigest()))
        except OSError as exc:result.append((str(path),'unreadable',type(exc).__name__,exc.errno))
    return tuple(result)

def prepare_existing_editor(project):
    from . import editing
    before=source_stamp(project)
    try:
        doc=editing.current(project)
        # Older projects still use the established import path, on explicit commit.
        packet=None if doc is None else {'doc':doc}
    except (OSError,KeyError,ValueError) as exc:
        packet={'unavailable':str(exc)}
    if packet is not None and 'doc' in packet:
        try:
            packet['preview_identity_path']=str(editing.preview_identity_path(project,doc))
        except Exception:
            # Optimization only: retain the original main-thread failure behavior.
            # Never turn preview computation failures into read-only editor fallback.
            pass
    if before!=source_stamp(project):raise ValueError('项目在读取期间发生了变化，请重新打开。')
    return {'path':str(project),'stamp':before,'editor':packet}
