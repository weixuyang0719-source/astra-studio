"""Lifecycle for a model worker leased to one Formal 4 GUI session."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
from threading import Event, Thread
from time import monotonic, sleep


def windowless_python(executable):
    """Prefer the same venv's GUI launcher for a background task on Windows."""
    if os.name != "nt":
        return executable
    python = Path(executable)
    if not python.is_absolute() or python.name.casefold() != "python.exe":
        return executable
    windowless = python.with_name("pythonw.exe")
    if not windowless.is_file():
        return executable
    return windowless if isinstance(executable, Path) else os.fspath(windowless)


def enabled():
    return (os.name == 'nt' and os.environ.get('ASTRA_DISABLE_PERSISTENT_WORKERS') != '1'
            and bool(re.fullmatch(r'[1-9][0-9]*-[a-f0-9]{32}', os.environ.get('ASTRA_MODEL_SESSION', ''))))


def leased_path(path, *route):
    if not enabled():
        return path
    route_key=hashlib.sha256(repr(route).encode()).hexdigest()[:12]
    return path.with_name(f'{path.stem}-{os.environ["ASTRA_MODEL_SESSION"]}-{route_key}.json')


@contextmanager
def start_lock(path):
    import msvcrt
    lock=path.with_suffix('.start')
    with lock.open('a+b') as stream:
        deadline=monotonic()+29
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                break
            except OSError:
                if monotonic() >= deadline:
                    yield False
                    return
                sleep(.08)
        try:
            yield True
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


def own_worker(endpoint, token):
    import ctypes
    from ctypes import wintypes
    if not enabled():
        raise RuntimeError('Model worker needs an active Astra session')
    pid=int(os.environ['ASTRA_MODEL_SESSION'].split('-',1)[0])
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=(wintypes.HANDLE,wintypes.DWORD)
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    handle=kernel.OpenProcess(0x00100000,False,pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    def cleanup_exit(code):
        try:
            if json.loads(endpoint.read_text(encoding='utf8')).get('token')==token:
                endpoint.unlink()
        except (OSError,ValueError):
            pass
        os._exit(code)

    def wait_owner():
        result=kernel.WaitForSingleObject(handle,0xffffffff)
        kernel.CloseHandle(handle)
        cleanup_exit(0 if result==0 else 1)

    Thread(target=wait_owner,daemon=True,name='astra-model-owner').start()
    return cleanup_exit


def read_request(conn, deadline=5, limit=8*1024*1024):
    # Bound both total time and size, rather than trusting each recv timeout.
    stop=monotonic()+deadline
    data=bytearray()
    while len(data)<limit:
        conn.settimeout(max(.001,stop-monotonic()))
        block=conn.recv(min(65536,limit-len(data)))
        if not block:
            return None
        data.extend(block)
        if b'\n' in block:
            first,_,extra=data.partition(b'\n')
            if extra:
                raise ValueError('Only one request is allowed per connection')
            conn.settimeout(5)
            return json.loads(first)
        if monotonic()>=stop:
            raise TimeoutError('Model request deadline exceeded')
    raise ValueError('Model request size exceeded')


@contextmanager
def active_client(conn, abort):
    done=Event()
    def watch():
        while not done.is_set():
            try:
                ready,_,_=select.select([conn],[],[],.2)
                if not ready:
                    continue
                if conn.recv(1,socket.MSG_PEEK):
                    return
            except OSError:
                pass
            if not done.is_set():
                abort(2)
            return
    thread=Thread(target=watch,daemon=True,name='astra-model-client')
    thread.start()
    try:
        yield done
    finally:
        done.set()
        thread.join(.4)
