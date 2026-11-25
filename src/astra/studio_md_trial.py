"""Read-only asynchronous MD trial bridge; no adoption route."""
import json
import time
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import Slot,QThreadPool,Qt
from . import md_beat_trial_service as service

class MdTrialBridge:
    @Slot(str,result=str)
    def mdBeatTrial(self,raw):
        try:
            data=json.loads(raw); action=data.get('action')
            if action=='start':
                if not self._project:raise ValueError('请先打开工程')
                if self.jobs.running or self._gpu_request or getattr(self,'_md_trial_busy',False):
                    raise ValueError('请等待当前任务完成后再试算')
                prepared=service.prepare(self._project['path'],data.get('base_revision'),data['request'])
                from .studio_bridge import _InputInspectionSignal,_InputInspectionTask
                if not hasattr(self,'_md_trial_signal'):
                    self._md_trial_signal=_InputInspectionSignal(self)
                    self._md_trial_signal.done.connect(self._md_trial_done,Qt.ConnectionType.QueuedConnection)
                token=uuid4().hex
                self._md_trial={'token':token,'prepared':prepared,'status':'running','started':time.monotonic()}
                self._md_trial_busy=True
                QThreadPool.globalInstance().start(_InputInspectionTask(prepared,token,self._md_trial_signal,service.run))
                return self._json({'ok':True,'token':token,'status':'running'})
            if action=='status':
                item=getattr(self,'_md_trial',None)
                if not item or item['token']!=data.get('token'):raise ValueError('试算已失效，请重新预览')
                if item['status']=='running' and time.monotonic()-item['started']>120:
                    item.update(status='timed_out',error='试算超过2分钟，已停止等待；后台可能仍在计算，工程未修改。请稍后重试。')
                from . import editing
                doc=editing.current(item['prepared']['project'])
                if (not self._project or str(Path(self._project['path']).resolve())!=item['prepared']['project']
                        or not doc or doc['revision']!=item['prepared']['base_revision']):
                    return self._json({'ok':True,'status':'stale','error':'工程已改变，旧预览已失效；实际拍点未修改'})
                return self._json({k:v for k,v in dict(item,ok=True).items() if k not in ('prepared','started')})
            raise ValueError('试验仅提供预览，不支持应用')
        except Exception as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str, object, str)
    def _md_trial_done(self,token,result,error):
        self._md_trial_busy=False
        item=getattr(self,'_md_trial',None)
        if self._closed or not item or token!=item['token'] or item['status']=='timed_out':return
        item.update(status='failed' if error else 'ready')
        if error:item['error']=error
        else:item['result']=result
