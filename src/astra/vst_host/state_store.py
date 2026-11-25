"""Atomic project-owned plugin-state files; never writes vendor preset folders."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

MAGIC = b'ASTRAVSTSTATE\x01'
MAX_STATE = 64 * 1024 * 1024

def save_state(path, *, class_id, state):
    if not class_id or not 0 < len(state) <= MAX_STATE:
        raise ValueError('Invalid plugin identity or state size')
    path = Path(path)
    metadata = json.dumps({'class_id':class_id,'sha256':hashlib.sha256(state).hexdigest()}, separators=(',', ':')).encode('utf-8')
    payload = MAGIC + struct.pack('<I', len(metadata)) + metadata + state
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def load_state(path, *, class_id):
    path = Path(path)
    if path.stat().st_size > MAX_STATE + 4096:
        raise ValueError('Plugin state file exceeds size limit')
    payload = path.read_bytes()
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC)+4:
        raise ValueError('Not an Astra plugin state file')
    size = struct.unpack_from('<I', payload, len(MAGIC))[0]
    start = len(MAGIC)+4
    if not 0 < size < 4096 or start+size >= len(payload):
        raise ValueError('Invalid plugin state metadata')
    metadata = json.loads(payload[start:start+size].decode('utf-8'))
    state = payload[start+size:]
    if metadata.get('class_id') != class_id:
        raise ValueError('State belongs to a different plugin class')
    if hashlib.sha256(state).hexdigest() != metadata.get('sha256'):
        raise ValueError('Plugin state is corrupt')
    return state
