"""Crash-contained plugin session control. No realtime callback crosses Python."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from time import monotonic_ns

from .editor_transport_wire import EVENT as EDITOR_SPACE_EVENT, EditorSpace

_MAX_RESPONSE = 100 * 1024 * 1024


class SessionError(RuntimeError):
    pass


class SetupProcessingRejected(SessionError):
    """Structured native setup rejection, not a crash or generic pipe error."""


def _load_error(plugin,role,stage,cause,*,exit_code=None,vendor=b'',category=None,grace=False,
                class_id=None,elapsed_ms=None):
    from .load_failure import receipt,record
    if isinstance(cause,SetupProcessingRejected):
        category='setup'
    details=receipt(plugin,role,stage,cause,exit_code,vendor,category or getattr(cause,'category',None))
    details['extended_for_activity']=grace
    if isinstance(class_id,str) and len(class_id)==32 and all(c in '0123456789abcdefABCDEF' for c in class_id):
        details['class_id']=class_id.upper()
    if elapsed_ms is not None:
        details['elapsed_ms']=elapsed_ms
    record(details)
    error=SetupProcessingRejected(details['short']) if isinstance(cause,SetupProcessingRejected) else SessionError(details['short'])
    error.load_diagnostic=details
    if hasattr(cause,'instrument_startup'):
        error.instrument_startup=cause.instrument_startup
    return error


def _drain_diagnostics(process):
    # Read bytes, including partial lines and native code-page text. A noisy
    # plugin cannot fill a pipe while the control reader is waiting for JSON.
    tail=bytearray()
    process.diagnostic_tail=tail
    def run():
        try:
            while True:
                block=process.stderr.buffer.read1(8192)
                if not block:
                    break
                tail[:]=(tail+block)[-8192:]
        except (OSError,ValueError):
            pass
        finally:
            process.stderr.close()
    threading.Thread(target=run,name='VST diagnostics',daemon=True).start()


def _spawn(arguments):
    from .child_command import child_command
    args = child_command('astra.vst_host.session_entry', *arguments)
    kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, encoding='utf-8', bufsize=1)
    job = None
    if sys.platform == 'win32':
        from uuid import uuid4
        from astra.desktop_worker import WindowsJob
        job_name = 'Local\\AstraVst-' + uuid4().hex
        job = WindowsJob(job_name)
        kwargs['env'] = dict(os.environ, ASTRA_VST_PRIVATE_JOB=job_name)
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        process=subprocess.Popen(args, **kwargs)
        _drain_diagnostics(process)
        return process, job
    except BaseException:
        if job is not None:
            job.close()
        raise


def _discard_standby(entry):
    if entry is None:
        return
    process, job = entry
    if job is not None:
        job.close()
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    process.stdout.close()


class PluginStandby:
    """At most one unassigned process. Only host code runs before selection."""
    def __init__(self):
        self._lock = threading.Condition()
        self._entry = None
        self._starting = self._closed = False

    def prime(self):
        with self._lock:
            if self._closed or self._entry or self._starting:
                return
            self._starting = True
        threading.Thread(target=self._prepare, name='VST standby', daemon=True).start()

    def _prepare(self):
        entry = None
        try:
            entry = _spawn(['--standby'])
        except (RuntimeError, OSError):
            pass  # An on-demand session may still be opened normally.
        finally:
            with self._lock:
                self._starting = False
                if not self._closed:
                    self._entry, entry = entry, None
                self._lock.notify_all()
            _discard_standby(entry)

    def take(self, arguments):
        with self._lock:
            # An immediate picker click can beat Popen's return. Join that
            # launch on the preparation thread, before any vendor code runs.
            if self._starting and not self._closed:
                self._lock.wait_for(lambda: not self._starting or self._closed, timeout=.4)
            entry, self._entry = self._entry, None
        if entry is not None:
            process, _ = entry
            try:
                if process.poll() is None:
                    process.stdin.write(json.dumps(arguments) + '\n')
                    process.stdin.flush()
                    return entry
            except OSError:
                pass
            _discard_standby(entry)
        return None

    def close(self):
        with self._lock:
            self._closed = True
            entry, self._entry = self._entry, None
            self._lock.notify_all()
        if entry is not None:
            threading.Thread(target=_discard_standby, args=(entry,), daemon=True).start()


class PluginSession:
    instrument_start_contract = 1
    def __init__(self, library, plugin, class_id, *, sample_rate=48000, max_block=512, timeout=None,
                 effect=False, instrument_setup=None, standby=None, on_started=None):
        args = [str(Path(library).resolve()), str(Path(plugin).resolve()), class_id, str(sample_rate),
                str(max_block), 'effect' if effect else 'instrument', str(os.getpid())]
        self._load_plugin,self._load_role,self._load_stage=plugin,args[5],'launch'
        self._load_class,self._load_start=class_id,monotonic_ns()
        if not Path(library).is_file() or not Path(plugin).exists():
            code='missing_host' if not Path(library).is_file() else 'missing_plugin'
            raise _load_error(plugin,self._load_role,'launch',ValueError(code),category=code,class_id=class_id)
        if instrument_setup is not None:
            if effect or len(instrument_setup)!=2 or any(type(n) is not int or not 0<=n<=32768 for n in instrument_setup):
                raise ValueError('Invalid instrument setup hints')
            args.extend(str(n) for n in instrument_setup)
        entry = standby.take(args) if standby is not None else None
        self.used_standby = entry is not None
        self._job_lock = threading.Lock()
        try:
            self.process, self._job = entry or _spawn(args)
        except OSError as exc:
            raise _load_error(plugin,self._load_role,'launch',exc,class_id=class_id) from exc
        self._adaptive_start=timeout is None
        self.timeout, self._next = 30 if timeout is None else timeout, 0
        self._startup_extended=False
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._editor_space = deque(maxlen=16)
        self._editor_space_seen = deque(maxlen=32)
        self._failure = None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            if on_started is not None:
                on_started(self)
            self.info = self.request('status')
        except Exception as exc:
            code=self.process.poll() if str(exc).startswith('Plugin process exited') else None
            self.terminate()
            for stream in (self.process.stdin,self.process.stdout):
                stream.close()
            if getattr(self,'_canceled_start',False):
                raise SessionError('已取消后台准备。') from exc
            raise self.load_error(exc,self._load_stage,code) from exc

    def cancel_startup(self):
        self._canceled_start=True
        self.terminate()

    def load_error(self,cause,stage,exit_code=None):
        return _load_error(self._load_plugin,self._load_role,stage,cause,exit_code=exit_code,
                           vendor=getattr(self.process,'diagnostic_tail',b''),grace=self._startup_extended,
                           class_id=self._load_class,elapsed_ms=(monotonic_ns()-self._load_start)//1_000_000)

    def _startup_result(self,waiter):
        from .load_failure import native_activity
        window=min(4,self.timeout/4)
        try:
            return waiter.get(timeout=self.timeout-window)
        except queue.Empty:
            before=native_activity(self)
        try:
            return waiter.get(timeout=window)
        except queue.Empty:
            after=native_activity(self)
            # Recent CPU or file/device traffic can justify one bounded grace
            # period during a large factory load. A sleeping modal or blocked
            # vendor with no progress keeps the normal deadline.
            if self._load_stage!='plugin' or not before or not after or (
                    after[0]-before[0]<2_500_000 and after[1]-before[1]<256*1024):
                raise
        self._startup_extended=True
        return waiter.get(timeout=self.timeout)

    def _disconnect(self, message):
        with self._pending_lock:
            if self._failure is not None:
                return
            self._failure = message
            pending, self._pending = self._pending, {}
            self._editor_space.clear()
        for waiter in pending.values():
            waiter.put_nowait(message if isinstance(message, Exception) else SessionError(message))

    def take_editor_space(self):
        with self._pending_lock:
            events = tuple(self._editor_space)
            self._editor_space.clear()
        return events

    def _read(self):
        failure = 'Plugin process exited'
        bad_protocol = False
        try:
            while True:
                line = self.process.stdout.readline(_MAX_RESPONSE + 1)
                if not line:
                    try: code = self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired: code = self.process.poll()
                    failure = f'Plugin process exited ({code if code is not None else "unknown"})'
                    return
                if len(line) > _MAX_RESPONSE:
                    failure, bad_protocol = 'Oversized plugin response', True
                    return
                try:
                    payload = json.loads(line)
                except (ValueError, RecursionError):
                    failure,bad_protocol='Malformed private plugin response',True
                    return
                if isinstance(payload, dict) and payload.get('event') == EDITOR_SPACE_EVENT:
                    event = EditorSpace.decode(payload)
                    with self._pending_lock:
                        if (event is not None and self._failure is None
                                and event not in self._editor_space_seen):
                            self._editor_space.append(event)
                            self._editor_space_seen.append(event)
                    continue  # Out-of-band UI edge; never acknowledges an RPC.
                if isinstance(payload, dict) and payload.get('startup_error') is True:
                    message = str(payload.get('error', 'Plugin initialization failed'))
                    failure = SetupProcessingRejected(message) if payload.get('code') == 'setup_processing_rejected' else SessionError(message)
                    if payload.get('code')=='host_binary':
                        failure.category='host_binary'
                    if isinstance(payload.get('instrument_startup'),dict): failure.instrument_startup=payload['instrument_startup']
                    return
                if isinstance(payload,dict) and set(payload)=={'startup_stage'} and not hasattr(self,'info'):
                    stage=payload['startup_stage']
                    if stage in ('host','plugin','handshake'):
                        self._load_stage=stage
                        continue
                if not isinstance(payload, dict) or 'request' not in payload or 'ok' not in payload:
                    failure,bad_protocol='Invalid private plugin response envelope',True
                    return
                ident = payload['request']
                with self._pending_lock:
                    if self._failure is not None:
                        return
                    waiter = None
                    if type(ident) is int and type(payload['ok']) is bool:
                        waiter = self._pending.pop(ident, None)
                if waiter is None:
                    failure, bad_protocol = 'Plugin protocol returned an invalid or unknown request ID', True
                    return
                waiter.put_nowait(payload)
        except (OSError, ValueError) as exc:
            failure = f'Plugin response pipe failed: {exc}'
        finally:
            self._disconnect(failure)
            if bad_protocol:
                try:
                    self.terminate()
                except (OSError, subprocess.TimeoutExpired):
                    pass  # callers have already been notified of the protocol failure

    def request(self, operation, **arguments):
        """Synchronous value IPC. Product control callers must use a background dispatcher.

        Writes are ordered, but replies can complete out of order (e.g. status
        while state_get is awaiting a native park ticket). Never hold the write
        lock while waiting for a reply, or the pending lock during pipe I/O.
        """
        waiter = queue.Queue(maxsize=1)
        live_insert_add = operation == 'insert_action' and arguments.get('action') == 'insert_add'
        candidate_insert = live_insert_add and arguments.get('candidate_only') is True
        insert_timeout=self.timeout * (2 if self._adaptive_start and live_insert_add else 1)
        deadline = None
        if candidate_insert:
            # A blocked vendor call may stop the worker from even reading the
            # full request. The watchdog must run outside its IPC write lock.
            deadline = threading.Timer(insert_timeout, self.terminate)
            deadline.daemon = True
            deadline.start()
        try:
            with self._write_lock:
                self._next += 1
                ident = self._next
                # An explicit panel action can pass foreground permission only to this worker.
                # Scan, status, restore, and transport never participate.
                if sys.platform == 'win32' and (operation == 'editor_open' or
                        operation == 'insert_editor' and arguments.get('open', True) is True):
                    from .editor_win32 import gesture_for,relay_gesture_for
                    from .editor_peer import verified_worker
                    relay=arguments.pop('_relay_editor_gesture',None)
                    with verified_worker(self.process, self.info.get('editor_worker_pid')) as peer:
                        gesture={}
                        if peer:gesture=relay_gesture_for(peer,relay) if relay is not None else gesture_for(peer)
                        arguments = dict(arguments, editor_gesture=gesture)
                if live_insert_add:
                    # Windows Python 3.10+ shares this monotonic clock across processes.
                    arguments = dict(arguments, prepare_deadline_ns=monotonic_ns() + int(insert_timeout * 1_000_000_000))
                try:
                    line = json.dumps(dict(request=ident, command=operation, arguments=arguments), allow_nan=False) + '\n'
                except (TypeError, ValueError, RecursionError) as exc:
                    raise SessionError('Plugin request must contain JSON values') from exc
                with self._pending_lock:
                    if self._failure is not None:
                        raise self._failure if isinstance(self._failure, Exception) else SessionError(self._failure)
                    if self.process.poll() is not None:
                        raise SessionError(f'Plugin process exited ({self.process.returncode})')
                    self._pending[ident] = waiter
                self.process.stdin.write(line)
                self.process.stdin.flush()
            # A slow add is not a dead session. Keep its request ID alive until the
            # worker definitively rejects preparation or acknowledges publication.
            if operation=='status' and not hasattr(self,'info') and self._adaptive_start:
                result=self._startup_result(waiter)
            else:
                result = waiter.get(timeout=None if live_insert_add and not candidate_insert else insert_timeout)
        except (OSError, ValueError, queue.Empty) as exc:
            if operation in ('snapshot_live_inserts', 'warm_insert_worker', 'prepare_favorite_effect') and isinstance(exc, queue.Empty):
                # Keep the ID registered until the normal reader consumes the
                # late reply. This is the audible source worker, not the trial.
                raise SessionError(f'{operation} timed out; current instrument retained') from exc
            error=SessionError('Plugin unresponsive; its dedicated process was stopped')
            error.category='timeout' if isinstance(exc,queue.Empty) else 'protocol'
            self._disconnect(error)
            self.terminate()
            raise error from exc
        finally:
            if deadline is not None:
                deadline.cancel()
                deadline.join()
        if isinstance(result, Exception):
            raise result
        if not result['ok']:
            raise SessionError(result.get('error', 'Plugin request failed'))
        return result.get('result')

    def close(self):
        try:
            if self.process.poll() is None:
                self.request('close')
                self.process.wait(timeout=5)
        except (SessionError, subprocess.TimeoutExpired):
            self.terminate()
        finally:
            self._close_job()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()

    def _close_job(self):
        with self._job_lock:
            if self._job is not None:
                self._job.close()
                self._job = None

    def terminate(self):
        # Last-resort process containment, not a native device release ack.
        self._disconnect('Plugin process stopped')
        self._close_job()
        if self.process.poll() is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
