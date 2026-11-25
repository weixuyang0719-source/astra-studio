"""Verify the Windows Studio/worker process pair. Windows venv can add one launcher hop."""
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
import os
import sys

k = C.WinDLL('kernel32', use_last_error=True)


def _fn(name, result, *args):
    f = getattr(k, name)
    f.restype, f.argtypes = result, args
    return f


class _Process(C.Structure):
    _fields_ = [('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',C.c_size_t),
                ('module',W.DWORD),('threads',W.DWORD),('parent',W.DWORD),('priority',W.LONG),
                ('flags',W.DWORD),('exe',W.WCHAR*260)]


_snapshot = _fn('CreateToolhelp32Snapshot', W.HANDLE, W.DWORD, W.DWORD)
_first = _fn('Process32FirstW',W.BOOL,W.HANDLE,C.POINTER(_Process))
_next = _fn('Process32NextW',W.BOOL,W.HANDLE,C.POINTER(_Process))
_open = _fn('OpenProcess',W.HANDLE,W.DWORD,W.BOOL,W.DWORD)
_wait = _fn('WaitForSingleObject',W.DWORD,W.HANDLE,W.DWORD)
_image = _fn('QueryFullProcessImageNameW',W.BOOL,W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD))
_close = _fn('CloseHandle',W.BOOL,W.HANDLE)


def _parent(pid):
    h = _snapshot(2,0)  # TH32CS_SNAPPROCESS; only read the parent identifier of this known PID.
    if not h or h == C.c_void_p(-1).value:
        return None
    try:
        row = _Process(); row.size = C.sizeof(row)
        ok = _first(h,C.byref(row))
        while ok:
            if row.pid == pid: return row.parent
            ok = _next(h,C.byref(row))
    finally:
        _close(h)


def _ours(h):
    size = W.DWORD(32768); path = C.create_unicode_buffer(size.value)
    if not _image(h,0,path,C.byref(size)): return False
    expected = {os.path.normcase(os.path.realpath(p)) for p in
                (sys.executable,getattr(sys,'_base_executable',None)) if p}
    return os.path.normcase(os.path.realpath(path.value)) in expected


@contextmanager
def verified_worker(launcher, claimed):
    """Hold the process object until the foreground grant returns (no recycled PID)."""
    if type(claimed) is not int or not 0 < claimed < 2**32 or launcher.poll() is not None:
        yield None
        return
    h = _open(0x101000,False,claimed)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION; read only
    try:
        valid = bool(h and _wait(h,0)==258 and
                     (claimed==launcher.pid or _parent(claimed)==launcher.pid) and _ours(h))
        yield claimed if valid else None
    finally:
        if h: _close(h)


def studio_for_worker(claimed):
    """Trust the spawning Studio or its one-hop Python launcher. Never trust a GUI HWND alone."""
    return verified_parent(os.getpid(),claimed)


def verified_parent(child,claimed):
    parent = _parent(child)
    if not parent:return 0
    if type(claimed) is not int or not 0 < claimed < 2**32: return 0
    if claimed == parent: return claimed
    h = _open(0x101000,False,parent)
    try:
        return claimed if h and _wait(h,0)==258 and _ours(h) and _parent(parent)==claimed else 0
    finally:
        if h: _close(h)
