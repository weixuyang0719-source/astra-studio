"""Recycle only a previously validated active automatic recovery file.
Caller holds the project edit lock and checks project/id/seal immediately before calling.
No permanent-delete fallback. Windows 8+ native helper is mandatory.
"""
import ctypes
import os
from pathlib import Path
import re
import stat

def recycle_verified(path, active, *, library=None):
    path, active = Path(path), Path(active)
    if not path.is_absolute() or not active.is_absolute():
        raise ValueError('Absolute backup paths required')
    if path.parent != active or not re.fullmatch(r'auto-[0-9a-f]{32}\.bak', path.name):
        raise ValueError('Not an active automatic backup')
    for p in (active, *active.parents, path):
        s=p.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400:
            raise ValueError('Reparse paths cannot be recycled')
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Backup is not a regular file')
    if os.name != 'nt':
        raise OSError('Recoverable backup rotation is unsupported on this OS')
    dll=ctypes.CDLL(str(library or Path(__file__).with_name('astra_recovery_recycle.dll')))
    fn=dll.astra_recycle_file
    fn.argtypes=[ctypes.c_wchar_p];fn.restype=ctypes.c_long
    hr=fn(str(path))
    if hr < 0:
        raise OSError(f'Recycle Bin operation failed: 0x{hr & 0xffffffff:08X}')
    if path.exists():
        raise OSError('Recycle Bin operation did not remove the source backup')
