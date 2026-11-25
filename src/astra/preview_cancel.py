"""Cancellation scoped to one preview worker, never a global process kill."""
from contextlib import contextmanager
from threading import local
import subprocess
import time

_state = local()

class PreviewCancelled(ValueError):
    pass

@contextmanager
def cancellation_scope(event):
    previous = getattr(_state, 'event', None)
    _state.event = event
    try:
        check_cancelled()
        yield
        check_cancelled()
    finally:
        _state.event = previous

def cancellation_active():
    return getattr(_state, 'event', None) is not None

def check_cancelled():
    event = getattr(_state, 'event', None)
    if event is not None and event.is_set():
        raise PreviewCancelled('Preview cancelled')

def run_process(args, *, timeout=None, capture_output=False, check=False, **kwargs):
    event = getattr(_state, 'event', None)
    if event is None:
        return subprocess.run(args, timeout=timeout, capture_output=capture_output, check=check, **kwargs)
    check_cancelled()
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Only this worker's own process is terminated. Other Studio/MuseScore
    # instances and formal-score jobs are unaffected.
    with subprocess.Popen(args, **kwargs) as process:
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                check_cancelled()
                remaining = None if deadline is None else deadline-time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining) if remaining is not None else .1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            check_cancelled()
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check: result.check_returncode()
        return result


def run_logged_process(args, log, *, inactivity_timeout=180, timeout=1800, **kwargs):
    """Stream child output while cancelling or terminating a silent child."""
    from queue import Queue, Empty
    from threading import Thread
    check_cancelled()
    pipe=Queue()
    last=''
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs) as process:
        def pump():
            try:
                for line in process.stdout:pipe.put(line)
            finally:
                pipe.put(None)
        reader=Thread(target=pump,daemon=True);reader.start()
        deadline=time.monotonic()+timeout
        idle=time.monotonic()+inactivity_timeout
        try:
            while True:
                check_cancelled()
                remaining=min(deadline,idle)-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(args,timeout if deadline<=idle else inactivity_timeout)
                try:line=pipe.get(timeout=min(.1,remaining))
                except Empty:continue
                if line is None:break
                if line.strip():
                    idle=time.monotonic()+inactivity_timeout
                    last=line.strip()
                    log(last)
            while process.poll() is None:
                check_cancelled()
                if time.monotonic()>=min(deadline,idle):raise subprocess.TimeoutExpired(args,inactivity_timeout)
                time.sleep(.05)
            check_cancelled()
            return process.returncode,last
        finally:
            if process.poll() is None:process.kill();process.wait()
            reader.join()
