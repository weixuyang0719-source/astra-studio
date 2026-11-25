from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import json
import os
import tempfile

PROTOCOL = 'astra-os-guard-v1'

class LockUnavailable(RuntimeError):
    pass


def _process_identity():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    # Linux boot identity plus kernel process start ticks, never wall-clock guesses.
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    stat = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    return boot + ':' + stat[19]


def _lock(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _valid(data, guard, identity):
    return (isinstance(data, dict) and data.get('protocol') == PROTOCOL
            and data.get('guard') == guard.name
            and data.get('guard_identity') == [identity.st_dev, identity.st_ino] and type(data.get('pid')) is int
            and data['pid'] > 0 and isinstance(data.get('created'), str) and bool(data['created'])
            and isinstance(data.get('nonce'), str) and len(data['nonce']) == 32
            and all(c in '0123456789abcdef' for c in data['nonce']))


def _read_marker(path):
    if path.is_symlink():
        raise LockUnavailable('Lock marker is a symbolic link; preserved.')
    try:
        with path.open('rb') as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise LockUnavailable('Unknown lock marker; preserved.')
        return raw, json.loads(raw)
    except (ValueError, UnicodeError):
        raise LockUnavailable('Legacy or damaged lock marker; preserved.') from None


@contextmanager
def owned_lock(path):
    """OS lock is authoritative. Stable guard is NEVER unlinked.

    Marker remains compatible with old O_EXCL writers. Only v1 markers can be
    replaced under the guard. No PID probing, kill, TTL or project-data recovery.
    """
    path = Path(path)
    guard = path.with_name(path.name + '.guard')
    if path.is_symlink() or guard.is_symlink() or path.parent.is_symlink():
        raise LockUnavailable('Symbolic-link lock path; preserved.')
    fd = os.open(guard, os.O_CREAT | os.O_RDWR, 0o600)
    temporary = None
    mine = None
    try:
        _lock(fd)  # Failure/permission denial is never interpreted as stale.
        identity = os.fstat(fd)
        location = guard.stat()
        if not identity.st_ino or (identity.st_dev, identity.st_ino) != (location.st_dev, location.st_ino):
            raise LockUnavailable('Guard identity changed; preserved.')
        mine = {'protocol': PROTOCOL, 'guard': guard.name, 'pid': os.getpid(),
                'created': _process_identity(), 'nonce': uuid4().hex,
                'guard_identity': [identity.st_dev, identity.st_ino]}
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(mine, sort_keys=True).encode('ascii'))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)  # Atomic, no partially written marker.
        except FileExistsError:
            old_raw, old = _read_marker(path)
            if not _valid(old, guard, identity):
                raise LockUnavailable('Unknown lock owner; preserved.')
            # OS guard excludes all v1 writers; old O_EXCL writers see marker.
            if path.read_bytes() != old_raw:
                raise LockUnavailable('Lock owner changed; preserved.')
            os.replace(temporary, path)  # Never open an acquisition gap.
        temporary.unlink(missing_ok=True)
        temporary = None
        yield
    finally:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if mine is not None:
                try:
                    _, current = _read_marker(path)
                    if current == mine:
                        path.unlink()
                except (FileNotFoundError, LockUnavailable):
                    pass
        finally:
            os.close(fd)  # Process death also releases the OS lock.
