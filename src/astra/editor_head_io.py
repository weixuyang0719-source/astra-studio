"""Windows mutable editor-head I/O. No retries or security-policy changes.

Readers share deletion; old handles retain the complete old file across replacement.
ReplaceFileW flags=0 preserves ACLs. Its WRITE_THROUGH flag is unsupported, so
flush the already-fsynced replacement again after publication. A post-publication
failure is deliberately raised for editing._save's authoritative-head observer.
"""
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from uuid import uuid4
import ctypes
from ctypes import wintypes as W
import os

@lru_cache(maxsize=1)
def _api():
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE];k.CreateFileW.restype=W.HANDLE
    k.CloseHandle.argtypes=[W.HANDLE];k.CloseHandle.restype=W.BOOL
    k.ReplaceFileW.argtypes=[W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,W.DWORD,ctypes.c_void_p,ctypes.c_void_p];k.ReplaceFileW.restype=W.BOOL
    k.FlushFileBuffers.argtypes=[W.HANDLE];k.FlushFileBuffers.restype=W.BOOL
    k.MoveFileExW.argtypes=[W.LPCWSTR,W.LPCWSTR,W.DWORD];k.MoveFileExW.restype=W.BOOL
    return k

def _open(path,access):
    h=_api().CreateFileW(str(path),access,7,None,3,0x80,None)
    if h==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    return h

@contextmanager
def open_reader(path):
    import msvcrt
    h=_open(path,0x80000000)
    try:fd=msvcrt.open_osfhandle(h,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        _api().CloseHandle(h);raise
    try:stream=os.fdopen(fd,'rb')
    except BaseException:
        os.close(fd);raise
    with stream:yield stream

def _flush_head(path):
    h=_open(path,0x40000000)
    try:
        if not _api().FlushFileBuffers(h):raise ctypes.WinError(ctypes.get_last_error())
    finally:_api().CloseHandle(h)

def publish(temporary,path,*,mutable):
    temporary,path=Path(temporary),Path(path)
    if mutable and path.exists():
        backup=path.with_name('.'+path.name+'.replace-backup-'+uuid4().hex)
        if not _api().ReplaceFileW(str(path),str(temporary),str(backup),0,None,None):
            error=ctypes.WinError(ctypes.get_last_error())
            # 1176/1177 can alter names. Never delete recovery material or retry
            # an edit whose publication outcome needs authoritative inspection.
            error.preserve_editor_temporary=True
            error.editor_recovery={'head':str(path),'temporary':str(temporary),'backup':str(backup),'outcome':'indeterminate'}
            raise error
        # Backup remains available if flush fails. Never roll back a new head.
        _flush_head(path)
        backup.unlink(missing_ok=True)
    else:
        # First head and immutable revision bodies must never overwrite a name.
        if not _api().MoveFileExW(str(temporary),str(path),8):
            raise ctypes.WinError(ctypes.get_last_error())
