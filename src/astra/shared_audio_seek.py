"""Continuous-output seeks; ASTRA_SMOOTH_SEEK=0 restores hard seeking.

Only the first five milliseconds at the exact destination are blended. Output
frame mappings retain the old audible coordinate until queued audio is played.
"""
import time, math
import numpy as np
from .shared_audio_core import Timeline, RATE, loop_position, loop_repeats
from .shared_audio_adapter import SharedAudioAdapter

class ContinuousTimeline(Timeline):
    def __init__(self,cache):
        super().__init__(cache);self.output_frames=0;self.segments=[(0,0,None)]
    def seek(self,frame):
        with self.lock:
            super().seek(frame);self.output_frames=0;self.segments=[(0,self.cursor,self.loop)];self.jump_at=240;self.ramp_at=0
    def played_position(self,output_frame):
        with self.lock:
            segment=self.segments[0]
            for candidate in self.segments:
                if candidate[0]>output_frame:break
                segment=candidate
            o,t,loop=segment;frame=t+max(0,output_frame-o)
            if loop:frame=loop_position(frame,loop,t)
            # Retain the currently heard segment and any future queued transitions.
            index=self.segments.index(segment)
            if index:self.segments=self.segments[index:]
            return frame
    def jump(self,frame):
        with self.lock:
            output=self.output_frames
            tail=self.render(240)
            self.output_frames=output;self.jump_tail=tail
            self.cursor=int(frame);self.jump_at=0;self.fade_at=240
            self.segments.append((output,self.cursor,self.loop))
    def render(self,count):
        with self.lock:
            result=super().render(count)
            at=getattr(self,'jump_at',240)
            if at<240:
                n=min(count,240-at);w=((np.arange(n)+at+1)/240).astype(np.float32)[:,None]
                result[:n]=self.jump_tail[at:at+n]*(1-w)+result[:n]*w
                self.jump_at+=n
            ramp=getattr(self,'ramp_at',240)
            if ramp<240:
                n=min(count,240-ramp)
                result[:n]*=((np.arange(n)+ramp+1)/240).astype(np.float32)[:,None]
                self.ramp_at+=n
            self.output_frames+=count
            return result

class ContinuousSeekAdapter(SharedAudioAdapter):
    timeline_type = ContinuousTimeline
    def close(self):
        self.pending_jump=None
        super().close()

    def position(self):
        fallback_target=getattr(self,'_fallback_seek_target',None)
        if fallback_target is not None:return fallback_target
        if self.active and self.started:
            value=self.timeline.played_position(round(self.sink.processedUSecs()*RATE/1000000))/RATE
            return min(self.duration,value) if not loop_repeats(self.loop) else value
        return super().position()
    def latest_unheard_target(self):
        pending=getattr(self,'pending_jump',None)
        if pending:return pending[0]
        if self.active and self.started:
            heard=round(self.sink.processedUSecs()*RATE/1000000)
            with self.timeline.lock:
                future=[t for o,t,loop in self.timeline.segments if o>heard]
                if future:return future[-1]/RATE
        return None
    def pause(self):
        target=self.latest_unheard_target();self.pending_jump=None
        super().pause()
        if target is not None:super().seek(target)
    def set_loop(self,start,end,enabled):
        self.pending_jump=None
        # Loop edits retain the established reset semantics; do not let an old
        # deferred seek commit into a newly defined loop.
        was_playing=self.playing
        if self.started:self.pause()
        super().set_loop(start,end,enabled)
        if self.active and not self.started:
            self.timeline.seek(round(self.position_hold*RATE))
        if was_playing:self.play()
    def select(self,key):
        self.pending_jump=None;super().select(key)
    def fallback(self,reason):
        target=self.latest_unheard_target()
        self.pending_jump=None
        # Preserve the latest destination, including a committed transition still
        # queued ahead of the audible clock, when transferring playing intent.
        self._fallback_seek_target=target
        try:super().fallback(reason)
        finally:self._fallback_seek_target=None
    def seek(self,seconds):
        if self.active and self.started and self.playing:
            seconds=float(seconds)
            if not math.isfinite(seconds):raise ValueError('invalid position')
            seconds=min(self.duration,max(0,seconds))
            if self.loop:seconds=loop_position(seconds,self.loop)
            self.pending_jump=(seconds,time.monotonic())
            return
        self.pending_jump=None
        super().seek(seconds)
    def _tick(self):
        request=getattr(self,'pending_jump',None)
        if self.active:
            # Keep current playback, loop boundary and the latest target resident.
            frames=[self.timeline.cursor]
            if self.loop:frames.append(round(self.loop[0]*RATE))
            if request:frames.append(round(request[0]*RATE))
            keys={(i,s) for frame in frames for i in range(len(self.keys))
                  for s in range(max(0,frame//RATE),max(0,frame//RATE)+2)}
            self.cache.protect(keys)
        if request and self.active:
            seconds,at=request;frame=round(seconds*RATE);self.cache.prefetch(frame)
            with self.cache.lock:
                ready=all((i,s) in self.cache.data for i in range(len(self.keys)) for s in range(frame//RATE,(frame+RATE//2)//RATE+1))
            if ready:
                try:self.timeline.jump(frame)
                except BufferError:
                    self.cache.prefetch(self.timeline.cursor)
                else:
                    self.pending_jump=None
                    self.jump_latency=time.monotonic()-at
            if self.pending_jump and time.monotonic()-at>5:
                self.fallback('seek decode timeout');return
        # Read the Qt clock before taking the render lock: audio callbacks may
        # hold a Qt backend lock while entering Timeline.render.
        heard=round(self.sink.processedUSecs()*RATE/1000000) if self.started else 0
        with self.timeline.lock:
            self._awaiting_jump_audio=bool(self.started and any(o>heard for o,t,loop in self.timeline.segments))
        super()._tick()
