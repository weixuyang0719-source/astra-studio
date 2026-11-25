"""Non-blocking Qt controller for the desktop worker process."""

from .release_paths import data_root, runtime_env_root
from pathlib import Path
import json
import os
import signal
import sys
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .desktop_worker import MANAGED_JOB, WindowsJob


ROOT=data_root()


class JobController(QObject):
    log=Signal(str)
    stage=Signal(str)
    completed=Signal(str)
    provisionalScore=Signal(dict)
    separated=Signal(object)
    failed=Signal(str)
    cancelled=Signal()

    _worker_module='astra.desktop_worker'

    def __init__(self,parent=None):
        super().__init__(parent)
        self._process=None
        self._job=None
        self._log_path=None
        self._reset_events()

    @property
    def running(self):
        return self._process is not None

    @property
    def log_path(self):
        return self._log_path

    def _reset_events(self):
        self._stdout=b''
        self._stderr=b''
        self._worker_pid=None
        self._result=None
        self._failure=None
        self._cancel_requested=False
        self._cancel_release_confirmed=True
        self._cancel_drain_deadline=None

    def start(self,request):
        if self.running:
            raise RuntimeError('已有桌面任务正在运行')
        self._reset_events()
        token=uuid4().hex
        folder=ROOT/'.cache'/'jobs'/token
        self._log_path=folder/'events.jsonl'
        request_path=folder/'request.json'
        job_name=f'Local\\AstraDesktop-{token}'
        try:
            from .project_access import prepare_job
            request=prepare_job(request)
            folder.mkdir(parents=True)
            if request.get('workflow_phase'):
                from .task_products import begin_task
                request=begin_task(ROOT,folder,request)
            self._log_path.touch()
            request_path.write_text(json.dumps(request,ensure_ascii=False,allow_nan=False),encoding='utf-8')
            self._job=WindowsJob(job_name) if os.name=='nt' else None
        except (OSError,TypeError,ValueError,RuntimeError) as exc:
            self._log_diagnostic(str(exc))
            if (folder/'task-products.json').is_file():
                from .task_products import finish_task
                try:finish_task(ROOT,folder,'failed')
                except (OSError,ValueError) as error:self._log_diagnostic(str(error))
            self.failed.emit(str(exc))
            return

        process=QProcess(self)
        self._process=process
        if self._job:
            process.destroyed.connect(lambda *_args, job=self._job: job.close())
        env=QProcessEnvironment.systemEnvironment()
        source=str(Path(__file__).resolve().parents[1])
        env.insert('PYTHONPATH',os.pathsep.join(filter(None,(source,env.value('PYTHONPATH')))))
        env.insert('PYTHONIOENCODING','utf-8')
        env.insert(MANAGED_JOB,job_name)
        from .project_access import _JOB_KEY, _JOB_ENV
        env.remove(_JOB_ENV)
        if _JOB_KEY in request:env.insert(_JOB_ENV,token)
        process.setProcessEnvironment(env)
        process.setWorkingDirectory(str(ROOT))
        executable='Scripts/python.exe' if os.name=='nt' else 'bin/python'
        python=runtime_env_root(ROOT)/'.venv'/executable
        if not python.is_file():
            python=Path(sys.executable)
        if os.name=='nt':
            # QProcess supplies stdin/stdout pipes, so pythonw can run the
            # exact same JSONL worker without opening Windows Terminal.
            windowless=python.with_name('pythonw.exe')
            if windowless.is_file():
                python=windowless
        process.setProgram(str(python))
        process.setArguments(['-u','-m',self._worker_module,str(request_path)])
        process.readyReadStandardOutput.connect(self._drain_stdout)
        process.readyReadStandardError.connect(self._drain_stderr)
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._process_finished)
        process.start()

    def cancel(self):
        process=self._process
        if process is None or self._cancel_requested or self._result is not None:
            return
        self._cancel_requested=True
        self.stage.emit('正在取消…')
        # A worker that has not received start cannot acquire locks or launch children.
        process.closeWriteChannel()
        if self._job:
            try:
                self._job.terminate()
            except OSError as exc:
                self._log_diagnostic(str(exc))
                self._cancel_release_confirmed=False
                self._job.close()  # KILL_ON_JOB_CLOSE is the fallback.
        elif self._worker_pid is not None:
            try:
                os.killpg(self._worker_pid,signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _event(self,event):
        event_type=event.get('type')
        message=event.get('message')
        if event_type=='ready':
            pid=event.get('pid')
            if type(pid) is not int or pid<=0 or self._worker_pid is not None:
                return
            self._worker_pid=pid
            if not self._cancel_requested:
                self._process.write(b'start\n')
            self._process.closeWriteChannel()
        elif event_type=='stage' and isinstance(message,str) and not self._cancel_requested:
            self.stage.emit(message)
        elif event_type=='log' and isinstance(message,str):
            self.log.emit(message)
        elif event_type=='completed' and isinstance(event.get('project'),str):
            self._result=event['project']
        elif event_type=='provisionalScore' and isinstance(event.get('receipt'),dict) and not self._cancel_requested:
            self._result={'provisionalScore':event['receipt']}
        elif event_type=='separated' and isinstance(event.get('preview'),dict) and not self._cancel_requested:
            self._result=event['preview']
        elif event_type=='failed' and isinstance(message,str):
            self._failure=message

    def _drain_stdout(self):
        if self._process is not None:
            self._stdout+=bytes(self._process.readAllStandardOutput())
            lines=self._stdout.split(b'\n')
            self._stdout=lines.pop()
            for line in lines:
                self._stdout_line(line)

    def _stdout_line(self,line):
        message=line.decode('utf-8','replace').rstrip('\r')
        try:
            event=json.loads(message)
        except ValueError:
            event=None
        if isinstance(event,dict) and 'type' in event:
            self._event(event)
        elif message:
            self._log_diagnostic(message)

    def _drain_stderr(self):
        if self._process is not None:
            self._stderr+=bytes(self._process.readAllStandardError())
            lines=self._stderr.split(b'\n')
            self._stderr=lines.pop()
            for line in lines:
                self._log_diagnostic(line.decode('utf-8','replace').rstrip('\r'))

    def _log_diagnostic(self,message):
        if not message:
            return
        if self._log_path is not None:
            try:
                with self._log_path.open('a',encoding='utf-8') as handle:
                    handle.write(json.dumps({'type':'log','message':message},ensure_ascii=False)+'\n')
            except OSError:
                pass
        self.log.emit(message)

    def _process_error(self,error):
        if self._process is not None and error==QProcess.ProcessError.FailedToStart:
            self._failure=self._process.errorString()
            self._log_diagnostic(self._failure)
            self._process_finished(-1,QProcess.ExitStatus.CrashExit)

    def _cleanup_lock(self):
        if self._worker_pid is None or self._log_path is None:
            return
        from .process_locks import owned_lock, LockUnavailable
        try:
            metadata=json.loads((self._log_path.parent/'lock.json').read_text(encoding='utf-8'))
            path=Path(metadata['path'])
            if metadata['pid']==self._worker_pid and path.is_absolute() and path.name=='.running':
                candidates=[path]
                folder=path.parent/'editing'
                if not folder.is_symlink():candidates.append(folder/'.lock')
                for candidate in candidates:
                    try:
                        if candidate.exists():
                            # Never delete by PID: acquire the same OS guard and
                            # validate its versioned marker before reclamation.
                            with owned_lock(candidate):
                                pass
                    except (OSError,LockUnavailable) as exc:
                        self._log_diagnostic(f'保留无法安全回收的锁 {candidate.name}：{exc}')
        except FileNotFoundError:
            pass
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self._log_diagnostic(f'无法检查任务锁：{exc}')

    def _process_finished(self,exit_code,_status):
        process=self._process
        if process is None:
            return
        self._drain_stdout()
        self._drain_stderr()
        if self._stdout:
            self._stdout_line(self._stdout)
        if self._stderr:
            self._log_diagnostic(self._stderr.decode('utf-8','replace'))
        if (self._cancel_requested and self._job is not None and self._cancel_release_confirmed
                and self._log_path and (self._log_path.parent/'task-products.json').is_file()):
            # QProcess.finished covers the main worker only. Confirm its
            # Windows job has no ffmpeg/model descendants before cleanup.
            if self._cancel_drain_deadline is None:
                self._cancel_drain_deadline=monotonic()+2
            try:
                active=self._job.active_processes()
            except OSError as exc:
                self._log_diagnostic(str(exc))
                self._cancel_release_confirmed=False
            else:
                if active and monotonic()<self._cancel_drain_deadline:
                    QTimer.singleShot(25,self,lambda:self._process_finished(exit_code,_status))
                    return
                if active:
                    self._cancel_release_confirmed=False
        if self._job:
            self._job.close()
            self._job=None
        if self._cancel_release_confirmed:
            self._cleanup_lock()
        self._process=None
        process.deleteLater()
        if self._log_path is not None:
            from .task_products import finish_task
            state='completed' if self._result is not None else 'cancelled' if self._cancel_requested else 'failed'
            try:finish_task(ROOT,self._log_path.parent,state,released=self._cancel_release_confirmed)
            except (OSError,RuntimeError,ValueError) as exc:self._log_diagnostic(str(exc))
        if isinstance(self._result,dict) and 'provisionalScore' in self._result:
            if exit_code==0 and not self._cancel_requested and self._failure is None:
                self.provisionalScore.emit(self._result['provisionalScore'])
            else:
                self.failed.emit(self._failure or '临时谱面任务未正常结束。')
        elif isinstance(self._result,dict):
            self.separated.emit({**self._result,'origin':self._log_path.parent.name})
        elif self._result is not None:
            self.completed.emit(self._result)
        elif self._cancel_requested:
            self.cancelled.emit()
        else:
            self.failed.emit(self._failure or f'桌面任务进程异常退出（{exit_code}）')
