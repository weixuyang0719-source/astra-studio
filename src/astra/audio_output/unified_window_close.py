"""Isolated final-stage close coordinator, after existing user/job close gates.
No process exit, global event flush, synchronous wait, or forced native cleanup.
"""
from PySide6.QtCore import QObject,QTimer,Signal
class OutputWindowClose(QObject):
 finished=Signal()
 failed=Signal(str)
 def __init__(self,bridge,channel,page,parent=None):
  super().__init__(parent)
  self.bridge=bridge;self.channel=channel;self.page=page
  self.started=False;self.done=False;self.error=None;self.blocked=False;self.owners=[]
  self.timer=QTimer(self);self.timer.setInterval(10);self.timer.timeout.connect(self.poll)
 def _fail(self,exc):
  self.blocked=True;self.timer.stop()
  msg=str(exc)
  if msg!=self.error:self.error=msg;self.failed.emit(msg)
 def begin(self):
  try:return self._begin()
  except Exception as exc:self._fail(exc)
 def _begin(self):
  if self.started:return
  self.bridge._output_close_manages_vst=True
  if self.bridge.close() is False:
   self.bridge._output_close_manages_vst=False
   return False
  self.started=True
  # Break WebChannel's registration before Python/Qt owners can be collected.
  sessions=list(getattr(self.bridge,'_output_retiring',[]))
  current=getattr(self.bridge,'_output',None)
  if current:sessions.append(current)
  self.owners=list(getattr(self.bridge,"_output_owners",[]))
  self.owners.extend(h.owner for h in sessions if h.owner is not None and h.owner not in self.owners)
  self.channel.deregisterObject(self.bridge);self.page.setWebChannel(None)
  self.timer.start();self.poll()
  return True
 def poll(self):
  if self.done or not self.started or self.blocked:return
  try:self._poll()
  except Exception as exc:self._fail(exc)
 def _poll(self):
  if hasattr(self.bridge,'_vst_pump'):self.bridge._vst_pump()
  if hasattr(self.bridge,'_output_poll'):self.bridge._output_poll()
  for route in getattr(self.bridge,'_vst_retiring',()):
   if getattr(route,'close_step',0)==3 and not getattr(route,'released',False):
    msg=getattr(route,'notice',None) or 'VST output release unconfirmed'
    self._fail(msg)
    return
  for owner in self.owners:
   s=owner.snapshot()
   if not s.closed:return
   if not s.output_released:
    msg=s.error or 'Audio output release unconfirmed'
    if msg!=self.error:self.error=msg;self.failed.emit(msg)
    return # fail closed; never destroy a potentially live output owner
   if hasattr(owner,'thread') and not owner.thread.isFinished():return
  if (getattr(self.bridge,'_vst_retiring',[]) or getattr(self.bridge,'_output_retiring',[])
      or getattr(self.bridge,'_owned_caches',[]) or getattr(self.bridge,'_output_owners',[])):return
  if getattr(self.bridge,'_asio_panel',None) is not None:return
  if getattr(getattr(self.bridge,'_kept_device',None),'handle',None) is not None:return
  self.done=True;self.timer.stop();self.owners.clear();self.finished.emit()
