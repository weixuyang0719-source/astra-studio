"""One cancellable, low-priority preview ahead; never installs document state."""
from pathlib import Path
from threading import Event
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt
from .preview_cancel import cancellation_scope, check_cancelled
from .preview_identity import preview_cache_key


def context(bridge):
    project=bridge._project or {}; editor=project.get('editor') or {}
    return (project.get('path'),project.get('export'),editor.get('revision'),
            editor.get('preview_path'),editor.get('path'))


def candidate_midi(bridge):
    project=bridge._project or {}; editor=project.get('editor') or {}
    preferred=editor.get('preview_path') or editor.get('path')
    if preferred:return preferred
    return next((f['path'] for f in project.get('files',[]) if f.get('kind')=='midi'
                 and f.get('name')=='piano_raw.mid' and not f.get('historical')),None)


class _Worker(QRunnable):
    def __init__(self, ticket, cancel, signal, image_bytes=False):
        super().__init__(); self.ticket=ticket; self.cancel=cancel; self.signal=signal
        self.image_bytes=image_bytes
    def run(self):
        result=None; error=None
        try:
            from .preview_worker import prepare_preview, unpack_midi
            kind,path,index,key,identity=self.ticket
            with cancellation_scope(self.cancel):
                options={'image_bytes':True} if kind=='pdf' and self.image_bytes else {}
                result=prepare_preview(kind,Path(path),index,background=True,**options)
                check_cancelled()
                if kind=='midi':result=unpack_midi(result)
                check_cancelled()
        except Exception as exc:error=str(exc)
        try:self.signal.emit(self.ticket,self.cancel,result,error)
        except RuntimeError:pass


class PreviewPrefetch(QObject):
    done=Signal(object,object,object,object)
    def __init__(self, bridge):
        super().__init__(bridge); self.bridge=bridge; self.active=None; self.failed=set()
        self.pool=QThreadPool(self); self.pool.setMaxThreadCount(1)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(1500)
        self.timer.timeout.connect(self._start)
        self.monitor=QTimer(self);self.monitor.setInterval(100)
        self.monitor.timeout.connect(self._check_foreground)
        self.done.connect(self._done,Qt.ConnectionType.QueuedConnection)
        self.stats={'started':0,'cached':0,'discarded':0,'cancelled':0}
    def activity(self):
        """User work wins, including foreground preview and transport commands."""
        if self.active and not self.active[1].is_set():
            self.active[1].set(); self.stats['cancelled']+=1
        if not self.bridge._closed:self.timer.start()
    def close(self):
        self.timer.stop();self.monitor.stop()
        if self.active:self.active[1].set()
    def _busy(self):
        b=self.bridge
        return (b._closed or not b._project or b.jobs.running or
                getattr(b,'_rhythm_candidate_running',False) or
                any(getattr(b,name,None) for name in ('_score_range_active','_score_range_pending',
                    '_score_scope_check_active','_score_scope_check_pending')) or
                b._preview_midi_active or b._preview_pdf_active or
                b._media.isPlaying() or bool(b._midi_transport.get('playing')) or
                bool(getattr(b,'_shared_audio',None) and b._shared_audio.playing))
    def _check_foreground(self):
        if self.active and (self._busy() or self.active[0][-1]!=context(self.bridge)):
            self.activity();self.monitor.stop()
    def _candidates(self):
        b=self.bridge; identity=context(b)
        path=candidate_midi(b)
        if path:
            try:
                path=b._authorized_result(path,'midi'); key=preview_cache_key(path)
                if key is not None and key not in b._preview_midi_cache:yield ('midi',str(path),0,key,identity)
            except (OSError,ValueError):pass
        # Only one page ahead of the currently displayed PDF; no whole-score scan.
        if b._pdf_path and not b._pdf.get('loading'):
            index=b._pdf.get('index',0)+1
            if index<b._pdf.get('pages',0):
                try:
                    path=b._authorized_result(b._pdf_path,'pdf'); identity_key=preview_cache_key(path);key=(*identity_key,index) if identity_key is not None else None
                    if key is not None and key not in b._preview_pdf_cache:yield ('pdf',str(path),index,key,identity)
                except (OSError,ValueError):pass
    def _start(self):
        if self.bridge._closed:return
        if self.active or self._busy():self.timer.start();return
        ticket=next((t for t in self._candidates() if t not in self.failed),None)
        if not ticket:return
        self.failed={t for t in self.failed if t[-1]==ticket[-1]}
        cancel=Event();self.active=(ticket,cancel);self.stats['started']+=1
        image_bytes=getattr(self.bridge,'_score_preview_resources',False)
        self.pool.start(_Worker(ticket,cancel,self.done,image_bytes),-1);self.monitor.start()
    def _done(self,ticket,cancel,result,error):
        if self.active and self.active[1] is cancel:self.active=None;self.monitor.stop()
        b=self.bridge
        if b._closed:return
        kind,path,index,key,identity=ticket
        valid=not cancel.is_set() and identity==context(b)
        try:
            if valid:
                current=preview_cache_key(b._authorized_result(path,kind))
                valid=current is not None and key==(current if kind=='midi' else (*current,index))
        except (OSError,ValueError):valid=False
        if not valid:self.stats['discarded']+=1
        elif error:self.failed.add(ticket)
        else:
            cache=b._preview_midi_cache if kind=='midi' else b._preview_pdf_cache
            cache[key]=result
            while len(cache)>(3 if kind=='midi' else 6):cache.pop(next(iter(cache)))
            self.stats['cached']+=1
        self.timer.start()
