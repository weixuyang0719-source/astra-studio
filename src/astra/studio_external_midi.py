"""Private immutable MIDI handoff; never saves the working document."""
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from . import editing, live_owner

class _Write(QRunnable):
    def __init__(self, signal, token, frozen, path):
        super().__init__()
        self.signal, self.token, self.frozen, self.path = signal, token, frozen, path
    def run(self):
        error = ''
        try:
            editing.preview_path(self.path.parent, self.frozen.document(), prepared_path=self.path, _owned=True)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        try:
            self.signal.emit(self.token, error)
        except RuntimeError:
            pass

class ExternalMidi(QObject):
    done = Signal(str, str)
    def __init__(self, bridge, cache):
        super().__init__(bridge)
        self.bridge, self.cache, self.pending = bridge, Path(cache), None
        self.done.connect(self.finished)
    def notify(self, uri, status, message):
        import json
        self.bridge.externalMidiChanged.emit(json.dumps(dict(uri=uri,status=status,message=message),ensure_ascii=False))
    def request(self, uri):
        bridge = self.bridge
        project = bridge._project
        if not project or uri != getattr(bridge, '_editor_uri', None):
            raise ValueError('此编辑版本已不属于当前工程。')
        if self.pending:
            return
        owner = live_owner.retained(project['path'])
        if owner is None:
            raise ValueError('当前编辑结果尚未就绪。')
        frozen = owner.freeze()
        token = uuid4().hex
        path = self.cache / (token + '.mid')
        self.pending = (token, project, uri, path)
        self.notify(uri, 'pending', '正在准备当前 MIDI…')
        QThreadPool.globalInstance().start(_Write(self.done, token, frozen, path))
    @Slot(str, str)
    def finished(self, token, error):
        pending = self.pending
        if not pending or pending[0] != token:
            return
        self.pending = None
        _, project, uri, path = pending
        if self.bridge._project is not project or getattr(self.bridge, '_editor_uri', None) != uri:
            self.notify(uri, 'cancelled', '工程已切换，未打开 MIDI。')
            return
        if not error:
            try:
                self.bridge._open_local(path)
            except Exception as exc:
                error = str(exc) or type(exc).__name__
        self.notify(uri, 'error' if error else 'ready', error or '已交给系统默认程序打开。')
