"""Small, identity-bound Beat This activations, independent of decoding options."""
from pathlib import Path
import hashlib
import json
import os
import tempfile
import zipfile

import numpy as np


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def activation_identity(audio, checkpoint, upstream_version):
    return {'version': 1, 'source_sha256': _sha256(audio),
            'checkpoint_sha256': _sha256(checkpoint),
            'beat_this_version': upstream_version, 'fps': 50, 'float16': False}


def _validate(beat, downbeat):
    if (beat.ndim != 1 or downbeat.shape != beat.shape or not beat.size
            or beat.dtype.kind != 'f' or downbeat.dtype.kind != 'f'
            or not np.isfinite(beat).all() or not np.isfinite(downbeat).all()):
        raise ValueError('Beat activations must be nonempty, finite, matching float arrays')


def load_activations(path, identity):
    """Return a validated cache hit, or None for stale/incomplete cache files."""
    try:
        with np.load(path, allow_pickle=False) as saved:
            if json.loads(str(saved['identity'].item())) != identity:
                return None
            beat, downbeat = saved['beat'].copy(), saved['downbeat'].copy()
        _validate(beat, downbeat)
        return beat, downbeat
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        return None


def save_activations(path, identity, beat, downbeat):
    beat, downbeat = np.asarray(beat), np.asarray(downbeat)
    _validate(beat, downbeat)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with temporary.open('wb') as handle:
            np.savez_compressed(handle, identity=json.dumps(identity, sort_keys=True),
                                beat=beat, downbeat=downbeat)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
