"""GUI-owned generation guard for asynchronous media callbacks and pending starts."""
class MediaGeneration:
    @property
    def _pending_audio(self):
        if getattr(self,'_pending_audio_epoch',-1)!=getattr(self,'_media_epoch',0):return None
        return getattr(self,'_pending_audio_value',None)
    @_pending_audio.setter
    def _pending_audio(self,value):
        self._pending_audio_value=value
        self._pending_audio_epoch=getattr(self,'_media_epoch',0)
    def _media_invalidate(self):
        self._media_epoch=getattr(self,'_media_epoch',0)+1
        self._pending_audio=None
        self._media_bind_callbacks()
    def _media_bind_callbacks(self):
        for media,name,callback in getattr(self,'_media_connections',[]):
            try:getattr(media,name).disconnect(callback)
            except (RuntimeError,TypeError):pass
        self._media_connections=[]
        if getattr(self,'_closed',False):return
        media=self._media;epoch=getattr(self,'_media_epoch',0)
        for name,callback in self._audio_signal_pairs():
            def guarded(*args,_media=media,_epoch=epoch,_callback=callback):
                if getattr(self,'_closed',False) or self._media is not _media or getattr(self,'_media_epoch',0)!=_epoch:return
                _callback(*args)
            getattr(media,name).connect(guarded)
            self._media_connections.append((media,name,guarded))
