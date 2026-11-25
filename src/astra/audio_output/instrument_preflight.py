"""Read an observed block only from the same, still-owned fixed output route. Never opens a device."""
from copy import deepcopy

_DEVICE_FIELDS = ('backend','device_id','sample_rate','output_channels','native_selector','buffer_frames')

def _device_identity(route):
    return tuple(route.get(k) for k in _DEVICE_FIELDS) if isinstance(route,dict) else None

def preflight_ticket(old,desired):
    lane=getattr(old,'lane_id',None)
    current=(getattr(old,'epoch',None),lane) if lane else None
    # Nested selector/channel values must not share mutable data with the chooser.
    return deepcopy(_device_identity(desired)),current,live_block_proof(old,desired)

def ticket_current(ticket,old,desired):
    target,owner,proof=ticket
    return (target==_device_identity(desired) and
            (not owner or owner==(getattr(old,'epoch',None),getattr(old,'lane_id',None))) and
            (not proof or proof==live_block_proof(old,desired)))

def live_block_proof(old, desired):
    if not old or getattr(old,'retiring',False) or not isinstance(desired,dict): return None
    own,wire=getattr(old,'device',None),getattr(old,'wire',None)
    if not isinstance(own,dict) or not isinstance(wire,dict): return None
    if wire.get('phase')!='ready' or wire.get('epoch')!=old.epoch or not old.lane_id or wire.get('lane_id')!=old.lane_id: return None
    req=desired.get('buffer_frames')
    if type(req) is not int or not 0<=req<=16384: return None
    for k in _DEVICE_FIELDS:
        if k not in desired or desired[k]!=own.get(k): return None
    d=wire.get('device')
    if not isinstance(d,dict) or any(d.get(k)!=desired[k] for k in ('backend','device_id','sample_rate','output_channels')): return None
    n=d.get('buffer_frames')
    if type(n) is not int or not 1<=n<=16384: return None
    return old.epoch,old.lane_id,n
