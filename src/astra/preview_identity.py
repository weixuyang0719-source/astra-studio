"""Bounded content identities for MIDI/PDF preview caches, never audio files."""
import hashlib
from pathlib import Path

# Keep GUI content checks small; larger previews stay asynchronously uncached.
MAX_CACHE_BYTES=256*1024

def preview_cache_key(path):
    """Hash every lookup: same size/timestamp does not imply the same document.

    Large files bypass cache and speculative work rather than hash unbounded
    content on the GUI thread. They retain ordinary asynchronous foreground reads.
    This is a byte budget, not a latency guarantee on slow or remote storage.
    """
    path=Path(path)
    if path.stat().st_size>MAX_CACHE_BYTES:return None
    with path.open('rb') as stream:data=stream.read(MAX_CACHE_BYTES+1)
    if len(data)>MAX_CACHE_BYTES:return None
    return (str(path),len(data),hashlib.sha256(data).hexdigest())
