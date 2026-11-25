"""Read-only playback facade: does not expose editor/job/file-write slots."""
import json
from PySide6.QtCore import QObject,Signal,Slot
from .unified_studio_bridge import UnifiedStudioBridge
class ReadOnlyBackend(UnifiedStudioBridge):
 def _refresh_recent(self):self._recent=[]
 def _save_preferences(self):pass
 def _persist_preferences(self,updated):pass
class TrialFacade(QObject):
 stateChanged=Signal(str);playerChanged=Signal(str);midiChanged=Signal(str);logs=Signal(str);ncmChanged=Signal(str)
 allowed=frozenset(('playPause','stop','seek','volume','selectAudio','audition','auditionRate','selectAuditionSource'))
 def __init__(self,parent=None,sources=(),allow_raw=False):
  super().__init__(parent)
  self._backend=ReadOnlyBackend(self,raw=False,experimental_joint=False,allow_output_settings=allow_raw)
  for name in ('stateChanged','playerChanged','midiChanged','logs','ncmChanged'):
   signal=getattr(self._backend,name,None)
   if signal is not None:signal.connect(getattr(self,name).emit)
  self._backend._sources=[dict(row) for row in sources]
  self._backend._source=sources[0]['key'] if sources else None
  if sources:self._backend._audio=dict(sources[0],name='音频试用副本',peaks=[])
  self._backend._phase='音频试用：只读副本，不修改原工程'
 def __getattr__(self,name):
  backend=object.__getattribute__(self,'_backend')
  return getattr(backend,name)
 def accept_input(self,*args):self._backend._problem('此窗口只试播启动时复制的音频，不导入或修改工程。')
 @Slot(result=str)
 def snapshot(self):return self._backend.snapshot()
 @Slot(result=str)
 def poll(self):return self._backend.poll()
 @Slot(str)
 def dispatch(self,message):
  try:
   data=json.loads(message)
   if data.get('action') not in self.allowed:raise ValueError('此试用窗口仅提供音频播放；编辑、生成和保存请使用原应用。')
   if data.get('action')=='selectAuditionSource' and data.get('key')=='midi':raise ValueError('此轮只测试原曲与分轨音频。')
   return self._backend.dispatch(message)
  except (ValueError,TypeError,AttributeError) as exc:self._backend._problem(str(exc))
 @Slot(str,result=str)
 def setAudioOutputPreference(self,value):return self._backend.setAudioOutputPreference(value)
 def close(self):self._backend.close()
