"""Background-only score ornament review, opaque capability per visible project."""
from PySide6.QtCore import Slot
from pathlib import Path
from uuid import uuid4
from . import ornament_review as review


class OrnamentBridge:
    def _ornament_current(self,cap):
        from .pipeline import read_json
        from .score_artifacts import current_score_export
        if not self._project or str(Path(self._project['path']).resolve())!=cap['project']:return False
        try:
            root=Path(cap['project'])
            return (read_json(root/'editing'/'current.json')['revision']==cap['base_revision'] and
                    str(current_score_export(root))==cap['export'] and
                    review.stamp(root/'notation_ornaments.json')==cap['overlay_stamp'])
        except (OSError,ValueError,KeyError,TypeError):return False

    def _ornament_start(self,data):
        if not self._project:raise ValueError('请先打开钢琴工程。')
        if self.jobs.running or self._gpu_request or getattr(self,'_ornament_busy',False) or getattr(self,'_bar_phase_busy',False):
            raise ValueError('请等待当前任务完成，再审阅装饰候选。')
        if not isinstance(data,dict):raise ValueError('无效装饰审阅请求。')
        project=str(Path(self._project['path']).resolve());kind=data.get('action','open')
        if kind=='open':
            def work(_):
                cap=review.bind(project,data.get('base_revision'))
                return {'cap':cap,'public':review.public(cap)}
        else:
            old=getattr(self,'_ornament_request',None)
            if not old or old['status']!='ready' or data.get('token')!=old['token'] or not old.get('cap',{}).get('available'):
                raise ValueError('候选令牌已失效，请重新读取。')
            cap=old['cap']
            if not self._ornament_current(cap):raise ValueError('工程、编辑、导出或装饰决定已变化，请重新读取。')
            def work(_):
                if not self._ornament_current(cap):raise ValueError('工程已改变，未保存旧操作。')
                updated,result=review.action(cap,data)
                return {'cap':updated,'public':result}
        from PySide6.QtCore import QThreadPool,Qt
        from .studio_bridge import _InputInspectionSignal,_InputInspectionTask
        if not hasattr(self,'_ornament_signal'):
            self._ornament_signal=_InputInspectionSignal(self)
            self._ornament_signal.done.connect(self._ornament_done,Qt.ConnectionType.QueuedConnection)
        request={'token':uuid4().hex,'status':'running','project':project,'base_revision':data.get('base_revision')}
        self._ornament_request=request;self._ornament_busy=True
        QThreadPool.globalInstance().start(_InputInspectionTask(None,request['token'],self._ornament_signal,work))
        return {'ok':True,'token':request['token'],'status':'running'}

    @Slot(str, object, str)
    def _ornament_done(self,token,result,error):
        self._ornament_busy=False
        if self._closed:return
        request=getattr(self,'_ornament_request',None)
        if not request or request['token']!=token:return
        if error:request.update(status='failed',error=error);return
        cap=result['cap']
        if cap.get('available') and not self._ornament_current(cap):request.update(status='stale',error='工程已改变，旧预览未显示。');return
        request.update(status='ready',cap=cap,result=result['public'])

    def _ornament_status(self,token):
        request=getattr(self,'_ornament_request',None)
        if not request or token!=request['token']:return {'ok':True,'status':'stale','error':'候选已失效。'}
        if request.get('cap',{}).get('available') and not self._ornament_current(request['cap']):
            return {'ok':True,'status':'stale','error':'工程或决定已改变，请重新读取候选。'}
        return {'ok':True,'status':request['status'],**({'error':request['error']} if request.get('error') else {}),
                **({'result':request['result']} if request['status']=='ready' else {})}
