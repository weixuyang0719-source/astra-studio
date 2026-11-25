"""Retain actual audio-analysis identity when only the musical map is reused."""
import re
from pathlib import Path


def retain_beat_cache(seed, state, project):
    """Return a produced key, never a requested-but-unexecuted configuration.

    For relocation only, copy the record and its hash-matching activations. A
    legacy record can still provide raw beat snapping without an NPZ. We do not
    discover other keys or relabel unrelated analysis as the current result.
    """
    from .pipeline import read_json, save_json, fingerprint, _atomic_copy
    if not seed or not state:
        return None
    key=(state.get('analysis_keys') or {}).get('beat_this')
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',key):
        return None
    seed, project=Path(seed).resolve(),Path(project).resolve()
    record=seed/'cache'/f'beats-{key}.json'
    if not record.is_file() or not record.resolve().is_relative_to(seed):
        return None
    try:
        if record.stat().st_size>8*1024*1024:
            return None
        content=read_json(record)
        if not isinstance(content,dict) or not isinstance(content.get('beats'),list) or not content['beats']:
            return None
    except (OSError,ValueError,TypeError):
        # Optional evidence must not break a valid manual clock's regeneration.
        return None
    # A same-directory run retains its actual record and never touches evidence.
    if seed==project:
        return key
    target=project/'cache'/record.name
    save_json(target,content)
    evidence=content.get('bar_result') or {}
    expected=evidence.get('activation_sha256')
    activation=seed/'cache'/'beat'/'activations.npz'
    if (isinstance(expected,str) and re.fullmatch(r'[a-f0-9]{64}',expected)
            and activation.is_file() and activation.resolve().is_relative_to(seed)
            and activation.stat().st_size<=128*1024*1024
            and fingerprint(activation)==expected):
        destination=project/'cache'/'beat'/'activations.npz'
        destination.parent.mkdir(parents=True,exist_ok=True)
        _atomic_copy(activation,destination)
    return key
