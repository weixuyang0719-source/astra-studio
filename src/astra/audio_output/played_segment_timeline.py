"""Isolated audible-segment identity for the existing continuous timeline."""
from astra.shared_audio_seek import ContinuousTimeline
class PlayedSegmentTimeline(ContinuousTimeline):
 def __init__(self,cache):
  self._serial=0;self._segment_ids={}
  super().__init__(cache)
  self._register_current()
 def _register_current(self):
  self._serial+=1;self._segment_ids[id(self.segments[-1])]=self._serial
 def seek(self,frame):
  super().seek(frame);self._segment_ids={};self._register_current()
 def jump(self,frame):
  super().jump(frame);self._register_current()
 def played_cursor(self,output_frame):
  with self.lock:
   index=0
   for n,candidate in enumerate(self.segments):
    if candidate[0]>output_frame:break
    index=n
   segment=self.segments[index];o,t,loop=segment
   frame=t+max(0,output_frame-o);lap=0
   if loop:
    a,z=loop
    if a<z and frame>=z:
     lap=1+(frame-z)//(z-a);frame=a+(frame-z)%(z-a)
    elif a>z and t<a and frame>=z:
     lap=1;frame=a+frame-max(t,z)
   token=(self._segment_ids[id(segment)],int(lap))
   if index:
    self.segments=self.segments[index:]
    keep={id(s) for s in self.segments};self._segment_ids={k:v for k,v in self._segment_ids.items() if k in keep}
   return frame,token
 def played_position(self,output_frame):return self.played_cursor(output_frame)[0]
