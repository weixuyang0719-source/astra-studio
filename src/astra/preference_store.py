"""Small cross-process, conflict-aware preference updates, not project storage."""
from copy import deepcopy
import json
from .process_locks import owned_lock

_MISSING = object()
class PreferenceConflict(ValueError):
    pass

def _changes(before, after, prefix=()):
    for key in set(before) | set(after):
        old, new = before.get(key, _MISSING), after.get(key, _MISSING)
        if isinstance(new, dict) and (isinstance(old, dict) or old is _MISSING):
            yield from _changes({} if old is _MISSING else old, new, prefix+(key,))
        elif old != new:
            yield prefix+(key,), old, new

def _get(data, keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:return _MISSING
        data=data[key]
    return data

def update_preferences(path, before, after, writer):
    """Merge only local changes. Same-field stale writes fail rather than clobber."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with owned_lock(path.with_name(path.name+'.lock')):
        if path.exists():
            try:
                current=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(current,dict):raise ValueError()
            except (ValueError,UnicodeError):
                raise PreferenceConflict('设置文件无法读取，已保留原文件；本次未保存。') from None
        else:
            current=deepcopy(before)  # preserves legacy desktop.json migration
        merged=deepcopy(current)
        for keys,old,new in _changes(before,after):
            for depth in range(1,len(keys)):
                parent=_get(current,keys[:depth])
                if parent is not _MISSING and not isinstance(parent,dict):
                    raise PreferenceConflict('设置结构已改变，本次未保存。')
            disk=_get(current,keys)
            if disk != old and disk != new:
                raise PreferenceConflict('另一窗口已更改同一设置，请重新打开应用后再试。')
            node=merged
            for key in keys[:-1]:
                if not isinstance(node.get(key),dict):node[key]={}
                node=node[key]
            if new is _MISSING:node.pop(keys[-1],None)
            else:node[keys[-1]]=deepcopy(new)
        writer(path,merged)
        return merged
