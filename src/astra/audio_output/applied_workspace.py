"""Frozen native owner -> latest-only playback projection; never reads disk.
The worker owns only immutable snapshots; GUI owns the provider and bridge.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import json
import weakref
from PySide6.QtCore import QObject, QTimer
from .memory_midi import AppliedMidi, MidiHead, NoteRow, ControlRow, ChannelWord


@dataclass(frozen=True)
class Projection:
    score: AppliedMidi
    warnings: tuple = ()

def project_workspace(frozen, program=0):
    import mido
    data=frozen.playback()
    head=frozen.head
    stamp=tuple(head['stamp'])
    if tuple(data['stamp']) != stamp or data['project_id'] != head['project_id']:
        raise ValueError('MIDI snapshot identity mismatch')
    other=[]; warnings=[]
    for row in data.get('other_events') or ():
        fields=dict(row['message']);kind=fields.get('type')
        if kind in ('set_tempo','time_signature','end_of_track'):
            continue
        try:msg=mido.Message.from_dict(fields)
        except LookupError:
            mido.MetaMessage.from_dict(fields)  # score/text metadata is not audible
            continue
        if msg.type not in ('polytouch','program_change','aftertouch','pitchwheel'):
            warnings.append('Live native MIDI cannot send preserved '+msg.type+'; source event retained')
            continue
        word=sum(v << (8*i) for i,v in enumerate(msg.bytes()))
        other.append(ChannelWord(row['seconds'],word))
    return Projection(AppliedMidi(stamp[0],stamp[1],
        tuple(NoteRow(**n) for n in data['notes']),
        tuple(ControlRow(**c) for c in data['controls']),
        json.dumps(data['tempo'],allow_nan=False,separators=(',',':')).encode(),
        data['duration'],tuple(other),program),tuple(dict.fromkeys(warnings)))


class ProjectionQueue:
    """All methods on GUI thread. Executor reads one retained Frozen at a time."""
    def __init__(self, executor, current, changed, convert=project_workspace):
        self.executor,self.current,self.changed,self.convert=executor,current,changed,convert
        self.head=MidiHead(None,True);self.error=None;self.warnings=()
        self.future=None;self.queued=None;self.wanted=None;self.closed=False
    def provider(self):
        if self.error:raise ValueError(self.error)
        return self.head
    def submit(self, frozen, program=0):
        if self.closed:return
        key=(frozen.head['project_id'],tuple(frozen.head['stamp']))
        if key==self.wanted:return
        self.wanted=key;self.queued=(frozen,program,key);self.error=None
        self.head=MidiHead(self.head.snapshot,True)
        self.changed();self._start()
    def _start(self):
        if self.future is None and self.queued is not None:
            frozen,program,key=self.queued;self.queued=None
            self.future=(key,self.executor.submit(self.convert,frozen,program))
    def poll(self):
        if self.closed:return
        if self.future is not None and self.future[1].done():
            key,task=self.future;self.future=None
            if key==self.wanted and self.current(key):
                try:
                    value=task.result()
                    self.head=MidiHead(value.score,False);self.warnings=value.warnings;self.error=None
                except Exception as exc:self.error=str(exc) or type(exc).__name__
                self.changed()
            self._start()
    def close(self):
        self.closed=True;self.queued=None;self.wanted=None
        if self.future:self.future[1].cancel()
        self.executor.shutdown(wait=False,cancel_futures=True)


class WorkspaceMidiAdapter(QObject):
    def __init__(self, bridge, frozen, selector, current, program=0):
        super().__init__(bridge)
        self.bridge=weakref.ref(bridge);self.epoch=frozen.head['stamp'][0]
        self.selector=selector;self.closed=False
        self.queue=ProjectionQueue(ThreadPoolExecutor(max_workers=1,thread_name_prefix='Editor MIDI projection'),
                                   current,self._changed)
        self.provider=self.queue.provider  # stable identity used to detect detach
        bridge.attach_memory_midi(self.epoch,self.provider,selector=selector)
        self.timer=QTimer(self);self.timer.setInterval(10);self.timer.timeout.connect(self.poll)
        bridge.destroyed.connect(self.close)
        self.queue.submit(frozen,program);self.timer.start()
    def _changed(self):
        bridge=self.bridge()
        if bridge is not None and not self.closed:
            doc=(getattr(bridge,'_project',None) or {}).get('editor')
            if doc is not None:doc['playback_warnings']=list(self.queue.warnings)
            for warning in self.queue.warnings:logging.getLogger(__name__).warning(warning)
            bridge.memory_midi_changed(self.epoch)
    def set_workspace(self,frozen,program=0):
        self.queue.submit(frozen,program)
    def poll(self):
        bridge=self.bridge()
        if (bridge is None or getattr(bridge,'_closed',False) or
            getattr(bridge,'_mem_provider',None) is not self.provider or
            not bridge._edit_preview_selected()):
            self.close();return
        self.queue.poll()
    def close(self,*_):
        if self.closed:return
        self.closed=True;self.timer.stop();self.queue.close()
        bridge=self.bridge()
        if bridge is not None and getattr(bridge,'_mem_provider',None) is self.provider:
            bridge.detach_memory_midi(self.epoch)
