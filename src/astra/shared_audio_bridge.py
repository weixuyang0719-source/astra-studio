"""Explicit guarded routing helpers; no runtime method replacement.

The validated 1x comparison path is enabled unless ASTRA_SHARED_AUDIO=0. Other
modes, unavailable decoders/devices and faults keep the established backend.
"""
import os, threading
from PySide6.QtCore import QTimer, Slot

_import_ready=threading.Event()
_import_lock=threading.Lock()
_adapter_class=None
_continuous_adapter_class=None
_import_error=None
_import_started=False

def _prepare_module():
    global _import_started
    with _import_lock:
        if _import_started:return
        _import_started=True
    def load():
        global _adapter_class,_continuous_adapter_class,_import_error
        try:
            from .shared_audio_adapter import SharedAudioAdapter
            _adapter_class=SharedAudioAdapter
            from .shared_audio_seek import ContinuousSeekAdapter
            _continuous_adapter_class=ContinuousSeekAdapter
        except Exception as exc:_import_error=str(exc)
        finally:_import_ready.set()
    threading.Thread(target=load,name="shared-audio-import",daemon=True).start()

class SharedAudioBridge:
    def _shared_prepare(self):
        if os.environ.get('ASTRA_SHARED_AUDIO','1')!='1' or getattr(self,'_closed',False):return
        _prepare_module()
        timer=getattr(self,'_shared_prepare_timer',None)
        if timer is None:
            timer=QTimer(self);timer.setInterval(20);timer.timeout.connect(self._shared_prepare_tick)
            self._shared_prepare_timer=timer
        timer.start()

    @Slot()
    def _shared_prepare_tick(self):
        if getattr(self,'_closed',False):self._shared_prepare_timer.stop();return
        if not _import_ready.is_set():return
        self._shared_prepare_timer.stop()
        # Prewarm a paused project. Never interrupt legacy audio already started
        # by an early click while optional dependencies were loading.
        midi=getattr(self,'_midi_backend',None)
        midi_playing=bool(midi and midi.state().get('playing'))
        if not self._media.isPlaying() and not midi_playing and not (self._pending_audio and self._pending_audio[1]):
            self._shared_start()

    def _shared_start(self):
        engine = getattr(self, '_shared_audio', None)
        if engine is not None:
            return engine.active
        if (os.environ.get('ASTRA_SHARED_AUDIO', '1') != '1'
                or getattr(self, '_closed', False)
                or getattr(self, '_shared_audio_failed', False)
                or len(getattr(self, '_sources', [])) < 2
                or not any(s.get('key') == getattr(self, '_source', None) for s in self._sources)
                or getattr(self, '_audition_together', False)
                or getattr(self, '_audition_rate', 1) != 1):
            return False
        if not _import_ready.is_set():
            self._shared_prepare()
            return False
        if _adapter_class is None:
            self._shared_audio_reason=_import_error or 'optional audio dependencies unavailable'
            return False
        try:
            adapter=((_continuous_adapter_class or _adapter_class) if os.environ.get('ASTRA_SMOOTH_SEEK','1')=='1' else _adapter_class)
            engine = adapter(self, enabled=True)
        except Exception as exc:
            self._shared_audio_reason = str(exc)
            return False
        self._shared_audio = engine
        self._shared_audio_reason = engine.reason
        if engine.active:
            engine.timer.timeout.connect(self._shared_emit)
            self._shared_sync()
        return engine.active

    def _shared_leave(self, reason, preserve=True):
        engine = getattr(self, '_shared_audio', None)
        if engine is None:
            return
        # Clear first: transferring state must not route back into the adapter.
        self._shared_audio = None
        if not engine.active and engine.last_error:
            self._shared_audio_failed = True
        if engine.active and preserve:
            engine.fallback(reason)
        engine.close()
        self._shared_audio_reason = reason

    def _shared_sync(self):
        engine = getattr(self, '_shared_audio', None)
        if engine is None or not engine.active:
            return False
        gain = 0 if self._muted else self._volume / 100
        if gain != getattr(engine, '_bridge_gain', None):
            engine.sink.setVolume(gain)
            engine._bridge_gain = gain
        selection = (getattr(self, '_audition_range', None)
                     or ((self._project or {}).get('editor') or {}).get('range') or {})
        signature = (selection.get('start', 0), selection.get('end', engine.duration),
                     selection.get('loop', False))
        if signature != getattr(engine, '_bridge_loop', None):
            try:
                engine.set_loop(*signature)
            except (TypeError, ValueError, OverflowError):
                self._shared_leave('invalid loop settings')
                return False
            engine._bridge_loop = signature
        return engine.active

    @Slot()
    def _shared_emit(self):
        engine = getattr(self, '_shared_audio', None)
        if engine is not None and engine.active:
            self._player_tick()
