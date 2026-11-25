"""Studio-facing controls for stopped desktop jobs and verified checkpoints."""
import os
from pathlib import Path

from . import task_products
from .release_paths import data_root


class CancelledWorkflow:
    def _cancelled_job_state(self):
        cached=getattr(self,'_cancelled_jobs',None)
        if cached is None:
            self._update_cancelled_job_state(task_products.pending(data_root()))
        return self._cancelled_jobs

    def _update_cancelled_job_state(self,token=None):
        root=data_root()
        if token is not None:
            current=task_products.choice(root,token)
            if current is None:
                token=task_products.pending(root)
                current=task_products.choice(root,token) if token else None
        else:
            current=None
        legacy=self._legacy_recovery_root()
        interrupted=[{**row,'id':'local:'+row['id']} for row in task_products.recovery_history(root)]
        managed=self._managed_recovery_root()
        if managed:
            from . import managed_score_recovery
            interrupted.extend({**row,'id':'managed:'+row['id']} for row in managed_score_recovery.history(managed))
        if legacy:
            interrupted.extend({**row,'id':'previous:'+row['id']} for row in task_products.legacy_recovery_history(legacy))
        self._cancelled_jobs={'cancelled_run':current,'cancelled_history':task_products.history(root),
                              'interrupted_history':interrupted}

    def _legacy_recovery_root(self):
        value=os.environ.get('ASTRA_RECOVER_PREVIOUS_PROFILE')
        return Path(value) if value and Path(value)!=data_root() else None

    def _managed_recovery_root(self):
        value=os.environ.get('ASTRA_RECOVER_MANAGED_PROFILE')
        return Path(value) if value and Path(value)!=data_root() else None

    def _open_interrupted_job(self,token):
        if not isinstance(token,str):
            raise ValueError('任务标识无效。')
        if getattr(self,'_interrupted_open_token',None):
            return
        from uuid import uuid4
        from PySide6.QtCore import Qt, QThreadPool
        from .studio_bridge import _InputInspectionSignal, _InputInspectionTask
        serial=uuid4().hex
        self._interrupted_open_token=serial
        if not getattr(self,'_interrupted_open_signal',None):
            self._interrupted_open_signal=_InputInspectionSignal(self)
            self._interrupted_open_signal.done.connect(self._interrupted_job_ready,Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(_InputInspectionTask(token,serial,
            self._interrupted_open_signal,self._prepare_interrupted_job))
        self._publish()

    def _prepare_interrupted_job(self,token):
        scope,_,identity=token.partition(':')
        root=data_root()
        if scope=='local':
            return task_products.recover(root,identity)
        if scope=='managed':
            from . import managed_score_recovery
            previous=self._managed_recovery_root()
            if not previous:
                raise ValueError('上一版乐谱缓存的目录不可用。')
            return managed_score_recovery.copy_score(root,previous,identity)
        if scope=='previous':
            from . import pipeline
            from .run_config import prepare_run_config
            from .device_policy import apply_device_policy
            legacy=self._legacy_recovery_root()
            if not legacy:
                raise ValueError('原候选的任务目录不可用。')
            request=task_products.legacy_recovery_request(legacy,identity)
            config=prepare_run_config(request,pipeline.load_config(),pipeline.ROOT)
            requested=request['execution_device']
            config,_=apply_device_policy(config,requested,probe={
                'available':request['workflow_recovered_device']=='cuda','busy':False,'reason':'',
            },separator_only=True)
            return task_products.import_legacy_recovery(root,legacy,identity,
                separator_key=pipeline._analysis_keys(config)['separator'],
                separator_options=config['models']['separator'])
        raise ValueError('任务标识无效。')

    def _interrupted_job_ready(self,serial,token,error):
        if self._closed or serial!=getattr(self,'_interrupted_open_token',None):
            return
        self._interrupted_open_token=None
        try:
            self._update_cancelled_job_state()
            if error:
                self._problem(error)
            elif not self.jobs.running and not self._gpu_request:
                self._restore_kept_job(token)
        except (OSError,ValueError,RuntimeError,TypeError) as exc:
            self._problem(exc)
        self._publish()

    def _restore_kept_job(self,token):
        request,checkpoint=task_products.open_kept(data_root(),token)
        source=Path(checkpoint['source'])
        self._new()
        self.accept_audio(source)
        preview={'origin':checkpoint['origin'],'project':checkpoint['project'],
                 'source':checkpoint['source'],'duration':checkpoint['duration'],
                 'instruments':request['instruments'],'stem_paths':checkpoint['stems'],
                 'complete':checkpoint.get('complete',True)}
        self._request={**request,'workflow_phase':'separation'}
        if self._audio and self._audio.get('import_loading'):
            self._kept_open_pending=(self._audio.get('import_token'),preview)
        elif self._audio and not self._audio.get('import_error'):
            self._separation_completed(preview,auto_continue=False)

    def _stop_desktop_job(self):
        pending=self._gpu_request
        if pending and pending.get('workflow_phase')=='separation':
            # The probe cannot publish after the token is invalidated. No
            # worker or project was created at this point.
            self._gpu_request=self._gpu_preflight=None
            self._separation_workflow=None
            self._job_progress.finish()
            self._phase='已取消任务。'
            self._update_cancelled_job_state(task_products.cancel_preflight(data_root(),pending))
            self._publish()
        else:
            self.jobs.cancel()

    def _remember_desktop_cancellation(self):
        logfile=self.jobs.log_path
        if logfile and (logfile.parent/'task-products.json').is_file():
            self._update_cancelled_job_state(logfile.parent.name)

    def _cancelled_job_action(self,action,data):
        if self.jobs.running or self._gpu_request:
            return
        root=data_root()
        token=data.get('id')
        if action=='openCancelledJob':
            self._restore_kept_job(token)
        elif action=='openInterruptedJob':
            self._open_interrupted_job(token)
        elif action=='keepCancelledJob':
            task_products.keep(root,token)
            self._update_cancelled_job_state(token)
        else:
            task_products.clean(root,token,live_paths=self._cancelled_live_paths())
            self._update_cancelled_job_state(token)
        self._publish()

    def _kept_audio_ready(self,token):
        pending=getattr(self,'_kept_open_pending',None)
        if not pending or pending[0]!=token:
            return
        self._kept_open_pending=None
        self._separation_completed(pending[1],auto_continue=False)

    def _cancelled_live_paths(self):
        paths={row['path'] for row in self._sources}
        paths.update(getattr(self,'_audio_bank',{}))
        for owner in getattr(self,'_output_owners',()):
            sample=owner.snapshot()
            if not (sample.closed and sample.output_released):
                paths.update(getattr(owner,'paths',()))
        return paths
