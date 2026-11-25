"""Read-only original pulses from the active provider, with cheap UI caching."""
from pathlib import Path

from .active_pulse_evidence import resolve


def _stamp(path):
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


class DetectedBeatCache:
    def __init__(self):
        self._root = None
        self._dependencies = []
        self._stamps = None
        self._value = None

    def snapshot(self, project):
        root = Path(project).resolve()
        if (root == self._root and self._stamps is not None
                and [_stamp(p) for p in self._dependencies] == self._stamps):
            return self._value
        dependencies = []
        value = dict(source='unavailable', cache_key=None, seconds=[], available=False,
                     reason='原始检测证据不可用；不会以编辑后的速度锚点或其它模型代替。')
        try:
            evidence = resolve(root, dependencies=dependencies)
            value.update(source=evidence['source'], provider=evidence['provider'],
                cache_key=evidence['cache_key'], seconds=evidence['seconds'],
                downbeat_seconds=evidence['downbeat_seconds'],
                evidence_ref=evidence['evidence_ref'], result_sha256=evidence['result_sha256'],
                available=True, reason='')
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            value['reason'] = str(exc) or value['reason']
        self._root = root
        self._dependencies = list(dict.fromkeys(dependencies))
        self._stamps = [_stamp(p) for p in self._dependencies]
        self._value = value
        return value
