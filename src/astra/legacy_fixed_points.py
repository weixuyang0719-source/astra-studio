"""Read-only retirement of obsolete editor pins; never touches tempo anchors.

Historical revision bytes stay intact. New revisions retain pin metadata solely
for provenance, not as constraints. Undo loads through the same adapter.
"""
from copy import deepcopy

def retire_fixed_points(document,in_place=False):
    # in_place: the caller owns a freshly parsed document (editing.current reads it from disk on
    # every call). Deep-copying the whole project there cost ~35 ms per call, twice per undo.
    result=document if in_place else deepcopy(document)
    old=result.get('anchors') or []
    if old:
        archived=result.setdefault('legacy_fixed_points',[])
        for pin in old:
            if pin not in archived:archived.append(pin if in_place else deepcopy(pin))
    result['anchors']=[]
    return result
